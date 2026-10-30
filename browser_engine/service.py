"""Stateful service behind the MCP tools. Keeps one browser tab open across tool calls.

All methods are synchronous and must be called from the same thread (Playwright's sync API);
mcp_server.py takes care of that.
"""
import os
import re
import sys
import urllib.request

from .cache import ActionCache, norm_intent
from .config import Config
from . import human
from .engine import BudgetExceeded, Engine, HumanNeeded, StepFailed, StepLog, normalize_steps
from .observer import observe, page_key
from .report import print_report, summarize  # noqa: F401  (print_report kept for CLI parity)
from .popups import PopupMemory
from .safety import Blocked, Guard

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

    def _engine(self, page, log, confirm_irreversible):
        return Engine(page, self.cfg, self.cache, log, local=self._get_local(),
                      claude=self._get_claude(), guard=Guard(confirm_irreversible), popups=self.popups)

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
            "cached_actions": len(self.cache.list()),
            "hints": hints,
        }

    def run_plan(self, goal, steps, start_url=None, confirm_irreversible=False):
        try:
            return self._run_plan(goal, steps, start_url, confirm_irreversible)
        except Exception as e:
            if "closed" not in str(e).lower() and "target" not in str(e).lower():
                raise
            self.page = None  # tab or browser went away mid-run: one clean retry
            return self._run_plan(goal, steps, start_url, confirm_irreversible)

    def _run_plan(self, goal, steps, start_url=None, confirm_irreversible=False):
        steps = normalize_steps(steps)
        page = self._get_page()
        log = StepLog(self.cfg.log_path)
        eng = self._engine(page, log, confirm_irreversible)
        if start_url:
            eng.target_url = start_url
            page.goto(start_url, wait_until="domcontentloaded")
        done = 0
        try:
            for st in steps:
                eng.do_step(st["intent"], st["value"])
                done += 1
        except HumanNeeded as e:
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
            self.last_obs = observe(page, self.cfg.max_elements)
            blocked = isinstance(e, Blocked)
            log.write("run", ok=False, goal=goal, completed=done, blocked=blocked,
                      tiers=dict(eng.tiers_used), claude_tokens=eng.claude_tokens)
            return {
                "ok": False,
                "blocked": blocked,
                "error": str(e)[:400],
                "completed": done,
                "failed_step": failed,
                "remaining_steps": steps[done + 1:],
                "tiers": dict(eng.tiers_used),
                "popups_closed": eng.popups_closed,
                "extracted": eng.extracted,
                "page": compact_page(self.last_obs, failed["intent"], 25, self.cfg.max_elements),
                "next": (
                    "Ask the user to approve or do this step themselves. If they approve an irreversible "
                    "step, call do_browser_step with confirm_irreversible=true."
                    if blocked else
                    "Do this one step with do_browser_step (it teaches the cache), then call "
                    "run_browser_plan with remaining_steps. If the plan itself was wrong, fix it instead."
                ),
            }
        if eng.human_waits:
            log.write("human", note=f"resumed after {eng.human_waits} bot check(s)")
        log.write("run", ok=True, goal=goal, steps=len(steps), tiers=dict(eng.tiers_used),
                  claude_tokens=eng.claude_tokens)
        return {
            "ok": True,
            "steps": len(steps),
            "tiers": dict(eng.tiers_used),
            "claude_api_tokens": eng.claude_tokens,
            "human_checks_solved": eng.human_waits,
            "popups_closed": eng.popups_closed,
            "extracted": eng.extracted,
            "url": page.url,
            "title": page.title(),
        }

    def wait_human(self, timeout_s=50, resume_url=None):
        """Wait for the user to clear a bot check, then optionally reopen resume_url."""
        page = self._get_page()
        self.last_obs = None
        reason = human.detect(page)
        if reason:
            human.notify(page, reason)
            if not human.wait_until_clear(page, max(5, min(int(timeout_s), 55))):
                return {"resolved": False, "challenge": reason, "url": page.url,
                        "next": "Still waiting. Remind the user to solve it in Chrome, then call wait_for_human again."}
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

    def do_step(self, intent, action, ref=None, value=None, text=None, confirm_irreversible=False):
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
            return {"ok": False, "blocked": True, "error": str(e)}
        finally:
            self.last_obs = None  # refs are stale after any action
        learned = False
        if ok and entry:
            d, p = page_key(obs.url)
            self.cache.put(d, p, norm_intent(intent), entry)
            learned = True
        log.write("step", intent=norm_intent(intent), tier="manual", ok=ok, note=why)
        out = {"ok": ok, "note": why, "learned": learned, "url": page.url}
        if eng.extracted:
            out["extracted"] = eng.extracted
        return out

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
