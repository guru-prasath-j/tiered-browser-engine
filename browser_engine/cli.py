import argparse
import json
import os
import sys

from .cache import ActionCache
from .config import Config
from .engine import BudgetExceeded, Engine, StepFailed, StepLog
from .observer import observe
from .safety import Blocked, Guard
from .report import DEFAULT_BASELINE_PER_STEP, print_report, summarize


def _load_plan(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["steps"] if isinstance(data, dict) else data


def _open_page(pw, args, cfg):
    """Returns (page, closer). With --cdp we attach to your own Chrome and never close it."""
    if args.cdp:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return page, (lambda: None)
    kwargs = dict(headless=args.headless, viewport={"width": 1280, "height": 800})
    if args.chrome:
        kwargs["channel"] = "chrome"
    ctx = pw.chromium.launch_persistent_context(os.path.expanduser(args.profile or cfg.profile_dir), **kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return page, ctx.close


def cmd_run(args, cfg):
    from playwright.sync_api import sync_playwright

    from .llm import Claude, LocalLLM

    if args.budget:
        cfg.max_claude_tokens = args.budget
    plan = _load_plan(args.plan) if args.plan else None
    local = None if args.no_local else LocalLLM(cfg)
    claude = None if args.no_claude else Claude(cfg)
    cache = ActionCache(cfg.cache_path)
    log = StepLog(cfg.log_path)

    with sync_playwright() as pw:
        page, close = _open_page(pw, args, cfg)
        engine = Engine(page, cfg, cache, log, local=local, claude=claude,
                        guard=Guard(confirm_irreversible=args.confirm_irreversible))
        try:
            print(json.dumps(engine.run(args.goal, start_url=args.url, plan=plan), indent=2))
            return 0
        except (StepFailed, BudgetExceeded, Blocked) as e:
            # Escalation report: enough context for Cowork / Claude Code to take over.
            try:
                page_text = observe(page, cfg.max_elements).text
            except Exception:
                page_text = None
            print(json.dumps({
                "run_id": log.run_id, "ok": False, "error": str(e),
                "tiers": dict(engine.tiers_used), "claude_tokens": engine.claude_tokens,
                "page": page_text,
            }, indent=2))
            return 2
        finally:
            close()


def cmd_report(args, cfg):
    print_report(summarize(cfg.log_path, args.baseline_per_step, args.run_id))
    return 0


def cmd_cache(args, cfg):
    cache = ActionCache(cfg.cache_path)
    if args.op == "clear":
        cache.clear(args.domain)
        print("cleared" + (f" {args.domain}" if args.domain else " everything"))
    else:
        for domain, page, intent, hits, fails, entry in cache.list(args.domain):
            e = json.loads(entry)
            print(f"{domain}{page}  [{intent}]  {e['action']}  hits={hits} fails={fails}  {e.get('selectors', [])[:1]}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="browser_engine", description="Token-saving tiered browser automation")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a goal")
    r.add_argument("goal")
    r.add_argument("--url", help="start URL")
    r.add_argument("--plan", help="JSON plan file (skips the Claude planner)")
    r.add_argument("--cdp", help="attach to your running Chrome, e.g. http://localhost:9222")
    r.add_argument("--profile", help="persistent browser profile dir (keeps logins)")
    r.add_argument("--chrome", action="store_true", help="use installed Google Chrome instead of bundled Chromium")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--no-local", action="store_true", help="skip the local LLM tier")
    r.add_argument("--no-claude", action="store_true", help="never call Claude (zero API spend)")
    r.add_argument("--budget", type=int, help="max Claude tokens for this run")
    r.add_argument("--confirm-irreversible", action="store_true",
                   help="allow submit/send/pay/delete-type clicks (only after you reviewed the plan)")

    rp = sub.add_parser("report", help="token usage vs screenshot baseline")
    rp.add_argument("--run-id")
    rp.add_argument("--baseline-per-step", type=int, default=DEFAULT_BASELINE_PER_STEP)

    c = sub.add_parser("cache", help="inspect or clear the action cache")
    c.add_argument("op", choices=["list", "clear"])
    c.add_argument("--domain")

    args = p.parse_args(argv)
    cfg = Config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    return {"run": cmd_run, "report": cmd_report, "cache": cmd_cache}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
