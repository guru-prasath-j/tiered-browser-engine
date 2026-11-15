"""Stateful service behind the MCP tools. Keeps one browser tab open across tool calls.

All methods are synchronous and must be called from the same thread (Playwright's sync API);
mcp_server.py takes care of that.
"""
import json
import os
import re
import sys
import time
import urllib.request

from .cache import ActionCache, norm_intent
from .config import Config
from . import forms, human
from .engine import BudgetExceeded, Engine, HumanNeeded, StepFailed, StepLog, normalize_steps
from .observer import observe, page_key
from .report import print_report, summarize  # noqa: F401  (print_report kept for CLI parity)
from .popups import PopupMemory
from .safety import Blocked, Guard
from . import logviz

STOP = {"the", "a", "an", "to", "into", "in", "on", "of", "and", "for", "click", "type", "select",
        "press", "open", "go", "field", "box", "button", "link", "page", "with", "first", "this"}


def _http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


LOAD_MORE = re.compile(r"\b(show|load|see|view) (\d+ )?more\b|more results|next page|^next$", re.I)


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _score(e, words):
    return 3 * len(words & _words(f"{e.name} {e.role}")) + (1 if e.in_view else 0)


def relevant_elements(obs, query, limit=30):
    """Top `limit` elements most related to `query`, kept in page order. Keeps tool output small."""
    words = _words(query) - STOP
    scored = [(_score(e, words), idx, e) for idx, e in enumerate(obs.elements)]
    top = sorted(scored, key=lambda t: (-t[0], t[1]))[:limit]
    return [e.line() for _, _, e in sorted(top, key=lambda t: t[1])]


def coverage(obs, query, limit, max_elements=150):
    """Tell the caller what it did NOT see, so a partial list is never mistaken for the whole page."""
    words = _words(query) - STOP
    out = {}
    if words:
        matching = [e for e in obs.elements if _score(e, words) >= 3]
        shown = min(len(matching), limit)
        out["matching"] = len(matching)
        if len(matching) > shown:
            out["matching_not_shown"] = len(matching) - shown
    more = [e.line() for e in obs.elements if LOAD_MORE.search(e.name or "")][:3]
    if more:
        out["load_more"] = more
    if len(obs.elements) >= max_elements:
        out["page_list_capped"] = True
    if out.get("matching_not_shown") or more or out.get("page_list_capped"):
        out["warning"] = ("You are NOT seeing every result. Before calling anything the cheapest, "
                          "lowest, best or 'all', raise limit, use the site's own sort/filter or "
                          "summary view, or click the load-more control and read again.")
    return out


def _prune(d):
    """Drop empty / zero fields: every key Claude reads costs tokens."""
    return {k: v for k, v in d.items() if v not in (None, [], {}, "", 0, False) or k in ("ok",)}


def compact_page(obs, query="", limit=30, max_elements=150):
    page = {
        "url": obs.url,
        "title": obs.title,
        "headings": obs.headings,
        "elements": relevant_elements(obs, query, limit),
        "total_elements": len(obs.elements),
    }
    page.update(coverage(obs, query, limit, max_elements))
    return page


class BrowserService:
    def __init__(self, cfg=None, local="auto", claude="auto", page_factory=None):
        self.cfg = cfg or Config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.cache = ActionCache(self.cfg.cache_path)
        self.popups = PopupMemory(self.cfg.popups_path) if self.cfg.dismiss_popups else None
        self.cdp = os.getenv("BE_CDP", "http://localhost:9222")
        self.page_factory = page_factory
        self.page = None
        self.last_obs = None
        self._pw = None
        self._local = local
        self._claude = claude
        self._site_memory = None
        self._vision = None if os.getenv("BE_VISION") != "0" else False
        self._session_logged = False
        logviz.maybe_prune(self.cfg)

    # ---- wiring ----------------------------------------------------------
    def _get_local(self):
        if self._local == "auto":
            self._local = None
            if os.getenv("BE_NO_LOCAL") != "1":
                try:
                    from .llm import LocalLLM

                    self._local = LocalLLM(self.cfg)
                except Exception as e:  # openai package missing etc.
                    print(f"[browser-engine] local LLM disabled: {e}", file=sys.stderr)
        return self._local

    def _get_claude(self):
        # The Claude API tier is OFF by default: in Cowork / Claude Code, Claude itself is the
        # fallback (via do_browser_step). Set BE_ALLOW_CLAUDE_API=1 to let the engine escalate.
        if self._claude == "auto":
            self._claude = None
            if os.getenv("BE_ALLOW_CLAUDE_API") == "1":
                from .llm import Claude

                self._claude = Claude(self.cfg)
        return self._claude

    def _page_alive(self):
        if self.page is None or self.page.is_closed():
            return False
        try:
            self.page.evaluate("1")  # catches tabs/browsers closed outside Playwright's view
            return True
        except Exception:
            return False

    def _get_page(self):
        if self._page_alive():
            return self.page
        self.page = None
        self.last_obs = None
        if self._pw is not None and not self.page_factory:
            # The CDP connection may be dead too (Chrome restarted): reconnect from scratch.
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        if self.page_factory:
            self.page = self.page_factory()
            return self.page
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        try:
            browser = self._pw.chromium.connect_over_cdp(self.cdp)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self.page = ctx.new_page()  # own tab: never hijack the tab the user is on
        except Exception as e:
            print(f"[browser-engine] CDP {self.cdp} unavailable ({e}); launching own browser",
                  file=sys.stderr)
            ctx = self._pw.chromium.launch_persistent_context(
                self.cfg.profile_dir, headless=False, viewport={"width": 1280, "height": 800}
            )
            self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return self.page

    def _get_vision(self):
        """Layer 2 (screenshots). Built once: probing which vision models exist costs a request."""
        if self._vision is None:
            from .vision import VisionLayer, providers

            try:
                self._vision = VisionLayer(providers(self.cfg), self.cfg.vision_layer_tries)
            except Exception as e:
                print(f"[browser-engine] screenshot layer disabled: {e}", file=sys.stderr)
                self._vision = False
        return self._vision or None

    def _engine(self, page, log, confirm_irreversible):
        return Engine(page, self.cfg, self.cache, log, local=self._get_local(),
                      claude=self._get_claude(), guard=Guard(confirm_irreversible), popups=self.popups,
                      vision=self._get_vision())

    # ---- tools -----------------------------------------------------------
    def status(self):
        local_ok = _http_ok(self.cfg.local_base_url.rstrip("/") + "/models")
        chrome_ok = _http_ok(self.cdp.rstrip("/") + "/json/version")
        hints = []
        if not local_ok:
            hints.append("Local LLM down: ask the user to run `OLLAMA_CONTEXT_LENGTH=8192 ollama serve`. "
                         "Cached steps still work.")
        if not chrome_ok:
            hints.append("Chrome not on CDP: ask the user to start Chrome with --remote-debugging-port=9222 "
                         "to reuse their logins; otherwise a separate browser window opens.")
        return {
            "local_llm": {"url": self.cfg.local_base_url, "model": self.cfg.local_model, "reachable": local_ok},
            "chrome_cdp": {"url": self.cdp, "reachable": chrome_ok},
            "claude_api_tier": os.getenv("BE_ALLOW_CLAUDE_API") == "1",
            "screenshot_layer": (self._get_vision().names() if self._get_vision() else []),
            "cached_actions": len(self.cache.list()),
            "saved_tasks": [r[0] for r in self.cache.list_tasks()[:20]],
            "hints": hints,
        }

    def run_plan(self, goal, steps=None, start_url=None, confirm_irreversible=False, task_name=None, values=None):
        if not steps and task_name:
            saved = self.cache.get_task(task_name)
            if not saved:
                return {"ok": False, "error": f"no saved task {task_name!r}; pass steps (it is saved when it succeeds)"}
            saved_url, steps = saved
            start_url = start_url or saved_url
            if values:  # {"<intent>": "<value>"} or {"<step number>": "<value>"} overrides
                for i, st in enumerate(steps):
                    for k in (st["intent"], str(i + 1), str(i)):
                        if k in values:
                            st["value"] = values[k]
                            break
        try:
            res = self._run_plan(goal, steps, start_url, confirm_irreversible)
        except Exception as e:
            if "closed" not in str(e).lower() and "target" not in str(e).lower():
                raise
            self.page = None  # tab or browser went away mid-run: one clean retry
            res = self._run_plan(goal, steps, start_url, confirm_irreversible)
        if res.get("ok") and task_name:
            self.cache.put_task(task_name, start_url, normalize_steps(steps))
            res["saved_as"] = task_name
        return res

    def _log_session(self):
        """Once per process: engine version and setup, so any log line can be traced to the code
        and models that produced it."""
        if self._session_logged:
            return
        self._session_logged = True
        vision = self._get_vision()
        env = {k: (v if not any(s in k for s in ("KEY", "TOKEN", "SECRET")) else "<set>")
               for k, v in os.environ.items() if k.startswith("BE_")}
        import platform

        StepLog(self.cfg.log_path, run_id=f"session-{os.getpid()}").write(
            "session", version=logviz.engine_version(), python=platform.python_version(),
            os=f"{platform.system()} {platform.release()} {platform.machine()}",
            mode=type(self).__name__, local_model=self.cfg.local_model, local_url=self.cfg.local_base_url,
            local_up=_http_ok(self.cfg.local_base_url.rstrip("/") + "/models"),
            vision_models=[getattr(c, "model", n) for n, c in (vision.providers if vision else [])],
            claude_api=os.getenv("BE_ALLOW_CLAUDE_API") == "1", cdp=self.cdp, env=env)

    def _run_plan(self, goal, steps, start_url=None, confirm_irreversible=False):
        steps = normalize_steps(steps)
        logviz.maybe_prune(self.cfg)
        self._log_session()
        log = StepLog(self.cfg.log_path)
        # Written first, so a run that hangs or crashes still shows its goal and plan.
        log.write("run_start", goal=goal, start_url=start_url, confirm=bool(confirm_irreversible) or None,
                  plan=[{"intent": s["intent"], "value": logviz.log_value(s["intent"], s["value"])}
                        for s in steps])
        try:
            return self._run_plan_inner(log, goal, steps, start_url, confirm_irreversible)
        except Exception as e:  # anything unexpected: log it with the traceback, then re-raise
            import traceback

            page = self.page
            if page is not None:
                try:
                    self._failure_shot(page, log, None, reason="crash", error=logviz.err_text(e))
                except Exception:
                    pass
            log.write("error", error=logviz.err_text(e, 800), type=type(e).__name__,
                      trace=traceback.format_exc()[-2000:])
            log.write("run", ok=False, goal=goal, crashed=True)
            raise

    def _run_plan_inner(self, log, goal, steps, start_url, confirm_irreversible):
        page = self._get_page()
        eng = self._engine(page, log, confirm_irreversible)
        if start_url:
            eng.target_url = start_url
            t_nav = time.time()
            try:
                resp = page.goto(start_url, wait_until="domcontentloaded")
            except Exception as e:
                log.write("nav", url=start_url, ok=False, ms=int((time.time() - t_nav) * 1000),
                          error=logviz.err_text(e))
                raise
            log.write("nav", url=start_url, final_url=page.url[:300] if page.url != start_url else None,
                      status=getattr(resp, "status", None), ok=True, ms=int((time.time() - t_nav) * 1000),
                      title=(page.title() or "")[:120])
        done = 0
        try:
            for st in steps:
                eng.do_step(st["intent"], st["value"])
                done += 1
        except HumanNeeded as e:
            self._failure_shot(page, log, steps[done]["intent"] if done < len(steps) else None,
                               reason="bot check", error=e.reason)
            log.write("run", ok=False, goal=goal, completed=done, needs_human=True,
                      tiers=dict(eng.tiers_used), claude_tokens=eng.claude_tokens)
            return {
                "ok": False,
                "needs_human": True,
                "challenge": e.reason,
                "completed": done,
                "remaining_steps": steps[done:],
                "resume_url": e.resume_url,
                "extracted": eng.extracted,
                "url": page.url,
                "next": ("A bot check (captcha / 'press and hold') is open in the user's Chrome and the tab "
                         "was brought to the front. Tell the user to solve it there, then call wait_for_human "
                         "with resume_url, then run_browser_plan with remaining_steps and NO start_url. "
                         "Never try to solve it yourself."),
            }
        except (Blocked, StepFailed, BudgetExceeded) as e:
            failed = steps[done]
            blocked = isinstance(e, Blocked)
            self._failure_shot(page, log, failed["intent"],
                               reason="blocked (needs approval)" if blocked else type(e).__name__,
                               error=str(e))
            self.last_obs = observe(page, self.cfg.max_elements)
            log.write("run", ok=False, goal=goal, completed=done, blocked=blocked,
                      tiers=dict(eng.tiers_used), claude_tokens=eng.claude_tokens)
            return _prune({
                "ok": False,
                "blocked": blocked,
                "error": str(e)[:240],
                "completed": done,
                "failed_step": failed,
                "remaining_steps": steps[done + 1:],
                "tiers": dict(eng.tiers_used),
                "popups_closed": eng.popups_closed,
                "extracted": eng.extracted,
                "page": compact_page(self.last_obs, failed["intent"], self.cfg.report_elements, self.cfg.max_elements),
                "next": ("needs user approval; then do_browser_step with confirm_irreversible=true" if blocked else
                         "do_browser_step for this step (with next_intent), then run_browser_plan remaining_steps"),
            })
        if eng.human_waits:
            log.write("human", note=f"resumed after {eng.human_waits} bot check(s)")
        log.write("run", ok=True, goal=goal, steps=len(steps), tiers=dict(eng.tiers_used),
                  claude_tokens=eng.claude_tokens)
        return _prune({
            "ok": True,
            "steps": len(steps),
            "tiers": dict(eng.tiers_used),
            "claude_api_tokens": eng.claude_tokens,
            "human_checks_solved": eng.human_waits,
            "popups_closed": eng.popups_closed,
            "extracted": eng.extracted,
            "url": page.url,
            "title": page.title(),
        })

    def _failure_shot(self, page, log, intent, reason=None, error=None):
        """Screenshot + 'shot' log line at the moment a step fails (shown in `browser-engine logs`)."""
        if os.getenv("BE_FAIL_SHOTS", "1") == "0":
            return
        name = logviz.save_failure_shot(page, self.cfg, log.run_id)
        try:
            url = page.url[:300]
        except Exception:
            url = None
        near = None
        try:  # the elements a person would look at for this step, to see what the engine saw
            obs = observe(page, self.cfg.max_elements)
            near = [e.line()[:140] for e in obs.subset(intent or "", self.cfg.report_elements).elements]
            n_els = len(obs.elements)
        except Exception as e:
            n_els = None
            near = [f"(could not read elements: {logviz.err_text(e, 150)})"]
        log.write("shot", file=name, intent=intent, url=url, reason=reason,
                  error=(error or "")[:500] or None, elements=near, n_elements=n_els)

    def wait_human(self, timeout_s=50, resume_url=None):
        """Wait for the user to clear a bot check, then optionally reopen resume_url."""
        page = self._get_page()
        self.last_obs = None
        log = StepLog(self.cfg.log_path)
        t0 = time.time()
        reason = human.detect(page)
        if reason:
            human.notify(page, reason)
            if not human.wait_until_clear(page, max(5, min(int(timeout_s), 55))):
                log.write("human_wait", challenge=reason, resolved=False, url=page.url[:300],
                          ms=int((time.time() - t0) * 1000))
                return {"resolved": False, "challenge": reason, "url": page.url,
                        "next": "Still waiting. Remind the user to solve it in Chrome, then call wait_for_human again."}
            log.write("human_wait", challenge=reason, resolved=True, url=page.url[:300],
                      ms=int((time.time() - t0) * 1000))
        if resume_url and not human.same_target(page.url, resume_url):
            page.goto(resume_url, wait_until="domcontentloaded")
            again = human.detect(page)
            if again:
                human.notify(page, again)
                return {"resolved": False, "challenge": again, "url": page.url,
                        "next": "The site asked again after reopening the page. Ask the user to solve it, then call wait_for_human (without resume_url)."}
        return {"resolved": True, "url": page.url, "title": page.title()}

    def page_elements(self, query="", limit=30):
        page = self._get_page()
        self.last_obs = observe(page, self.cfg.max_elements)
        return compact_page(self.last_obs, query, max(5, min(int(limit), 150)), self.cfg.max_elements)

    def do_step(self, intent, action, ref=None, value=None, text=None, confirm_irreversible=False,
                next_intent=None):
        page = self._get_page()
        obs = self.last_obs
        if obs is None or obs.url != page.url:
            return {"ok": False, "error": "page changed since the last element list; call get_page_elements first"}
        log = StepLog(self.cfg.log_path)
        eng = Engine(page, self.cfg, self.cache, log, guard=Guard(confirm_irreversible))
        eng.intent = intent
        action = (action or "").lower().strip()
        decision = {"action": action, "ref": ref, "text": text if text is not None else (value or "")}
        try:
            ok, why, entry = eng.execute_decision(decision, action, value, obs)
        except Blocked as e:
            log.write("step", intent=norm_intent(intent), tier="manual", ok=False, blocked=True,
                      note=str(e)[:300], action=action, ref=ref)
            return {"ok": False, "blocked": True, "error": str(e)}
        finally:
            self.last_obs = None  # refs are stale after any action
        learned = False
        if ok and entry:
            d, p = page_key(obs.url)
            self.cache.put(d, p, norm_intent(intent), entry)
            learned = True
        el = obs.by_ref.get(ref) if isinstance(ref, str) else None
        log.write("step", intent=norm_intent(intent), tier="manual", ok=ok, note=why, url=page.url[:300],
                  action=action, ref=ref, target=f'{el.role} "{el.name[:60]}"' if el else None,
                  value=logviz.log_value(intent, value if value is not None else text))
        if not ok:
            self._failure_shot(page, log, norm_intent(intent), reason="manual step failed", error=why)
        out = {"ok": ok, "note": why, "learned": learned, "url": page.url}
        if eng.extracted:
            out["extracted"] = eng.extracted
        # Hand back the elements for the next step right away, so Claude doesn't need a separate
        # get_page_elements call (one round trip and one element list saved per manual step).
        try:
            self.last_obs = observe(page, self.cfg.max_elements)
            if next_intent or not ok:
                out["page"] = compact_page(self.last_obs, next_intent or intent, self.cfg.report_elements,
                                           self.cfg.max_elements)
        except Exception:
            self.last_obs = None
        return _prune(out)

    def choose_option(self, question, option, confirm_irreversible=False):
        page = self._get_page()
        guard = Guard(confirm_irreversible)
        try:
            guard.check(f"answer: {question}", "click", option, {"label": str(option)})
        except Blocked as e:
            return {"ok": False, "blocked": True, "error": str(e)}
        res = forms.choose(page, question, option, self.cfg.settle_ms)
        self.last_obs = None
        StepLog(self.cfg.log_path).write("step", intent=norm_intent(f"answer: {question}"), tier="rule",
                                         ok=bool(res.get("ok")), note=str(res)[:200])
        return res

    def extract_listings(self, url=None, limit=20, query=""):
        from . import listings

        page = self._get_page()
        self.last_obs = None
        if self._site_memory is None:
            self._site_memory = listings.SiteMemory(listings.memory_path(self.cfg.data_dir))
        res = listings.extract(page, url, max(1, min(int(limit), 100)), query, self._site_memory)
        reason = human.detect(page)
        if reason:
            human.notify(page, reason)
            return {"ok": False, "needs_human": True, "challenge": reason, "resume_url": url or page.url}
        return res

    def tasks(self):
        return {"tasks": [{"name": n, "start_url": u, "steps": len(json.loads(st)), "runs": h}
                          for n, u, st, h in self.cache.list_tasks()]}

    def check_form(self):
        page = self._get_page()
        return forms.check_form(page)

    def report(self, run_id=None):
        return summarize(self.cfg.log_path, run_id=run_id) or {"note": "no runs logged yet"}

    def cache_op(self, op="list", domain=None):
        if op == "clear":
            self.cache.clear(domain)
            if self.popups:
                self.popups.clear(domain)
            return {"cleared": domain or "everything"}
        rows = self.cache.list(domain)
        return {"count": len(rows),
                "entries": [{"page": f"{d}{p}", "intent": i, "hits": h, "fails": f}
                            for d, p, i, h, f, _ in rows[:100]],
                "popup_buttons": ({k: v for k, v in self.popups.list().items() if not domain or k == domain}
                                  if self.popups else {})}
