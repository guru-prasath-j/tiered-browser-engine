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
from .engine import BudgetExceeded, Engine, StepFailed, StepLog, normalize_steps
from .observer import observe, page_key
from .report import print_report, summarize  # noqa: F401  (print_report kept for CLI parity)
from .safety import Blocked, Guard

STOP = {"the", "a", "an", "to", "into", "in", "on", "of", "and", "for", "click", "type", "select",
        "press", "open", "go", "field", "box", "button", "link", "page", "with", "first", "this"}


def _http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def relevant_elements(obs, query, limit=30):
    """Top `limit` elements most related to `query`, kept in page order. Keeps tool output small."""
    words = set(re.findall(r"[a-z0-9]+", (query or "").lower())) - STOP
    scored = []
    for idx, e in enumerate(obs.elements):
        name = set(re.findall(r"[a-z0-9]+", f"{e.name} {e.role}".lower()))
        score = 3 * len(words & name) + (1 if e.in_view else 0)
        scored.append((score, idx, e))
    top = sorted(scored, key=lambda t: (-t[0], t[1]))[:limit]
    return [e.line() for _, _, e in sorted(top, key=lambda t: t[1])]


def compact_page(obs, query="", limit=30):
    return {
        "url": obs.url,
        "title": obs.title,
        "headings": obs.headings,
        "elements": relevant_elements(obs, query, limit),
        "total_elements": len(obs.elements),
    }


class BrowserService:
    def __init__(self, cfg=None, local="auto", claude="auto", page_factory=None):
        self.cfg = cfg or Config()
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.cache = ActionCache(self.cfg.cache_path)
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
                      claude=self._get_claude(), guard=Guard(confirm_irreversible))

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
            page.goto(start_url, wait_until="domcontentloaded")
        done = 0
        try:
            for st in steps:
                eng.do_step(st["intent"], st["value"])
                done += 1
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
                "extracted": eng.extracted,
                "page": compact_page(self.last_obs, failed["intent"], 25),
                "next": (
                    "Ask the user to approve or do this step themselves. If they approve an irreversible "
                    "step, call do_browser_step with confirm_irreversible=true."
                    if blocked else
                    "Do this one step with do_browser_step (it teaches the cache), then call "
                    "run_browser_plan with remaining_steps. If the plan itself was wrong, fix it instead."
                ),
            }
        log.write("run", ok=True, goal=goal, steps=len(steps), tiers=dict(eng.tiers_used),
                  claude_tokens=eng.claude_tokens)
        return {
            "ok": True,
            "steps": len(steps),
            "tiers": dict(eng.tiers_used),
            "claude_api_tokens": eng.claude_tokens,
            "extracted": eng.extracted,
            "url": page.url,
            "title": page.title(),
        }

    def page_elements(self, query="", limit=30):
        page = self._get_page()
        self.last_obs = observe(page, self.cfg.max_elements)
        return compact_page(self.last_obs, query, max(5, min(int(limit), 150)))

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
            return {"cleared": domain or "everything"}
        rows = self.cache.list(domain)
        return {"count": len(rows),
                "entries": [{"page": f"{d}{p}", "intent": i, "hits": h, "fails": f}
                            for d, p, i, h, f, _ in rows[:100]]}
