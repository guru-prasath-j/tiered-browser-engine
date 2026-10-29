"""Benchmark: run bench/tasks.json cold (empty cache) then warm, and score each run against
ground truth instead of the engine's own 'ok'.

    python bench/run.py                 # local model + your Chrome on :9222 (or its own browser)
    python bench/run.py --passes 3      # cold + 2 warm passes

Writes bench/results.json and prints a markdown table.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.cache import ActionCache  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.engine import Engine, StepFailed, StepLog  # noqa: E402
from browser_engine.report import DEFAULT_BASELINE_PER_STEP  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def ground_truth(page, task, summary):
    """(passed, why) from the page and the extracted text alone."""
    c = task["check"]
    if c.get("url_regex") and not re.search(c["url_regex"], page.url):
        return False, f"url {page.url}"
    if c.get("extracted_regex"):
        text = "\n".join(x["text"] for x in summary.get("extracted") or [])
        if not re.search(c["extracted_regex"], text):
            return False, f"read {text[:60]!r}"
    for sel, prop, want in c.get("dom", []):
        got = page.locator(sel).first.evaluate(f"e => e.{prop}")
        if got != want:
            return False, f"{sel}.{prop}={got!r}"
    return True, "ok"


def run_task(page, cfg, cache, log, local, task):
    t0 = time.time()
    eng = Engine(page, cfg, cache, log, local=local, claude=None)
    try:
        s = eng.run(task["id"], start_url=task["url"], plan=task["steps"])
    except StepFailed as e:
        s = {"ok": False, "error": str(e)[:160], "tiers": dict(eng.tiers_used), "extracted": eng.extracted}
    passed, why = ground_truth(page, task, s)
    return {"id": task["id"], "engine_ok": bool(s.get("ok")), "correct": passed, "why": why,
            "tiers": dict(s.get("tiers") or eng.tiers_used), "seconds": round(time.time() - t0, 1),
            "steps": len(task["steps"])}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--tasks", default=os.path.join(HERE, "tasks.json"))
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright

    from browser_engine.llm import LocalLLM

    tasks = json.load(open(args.tasks))["tasks"]
    with tempfile.TemporaryDirectory() as d:
        cfg = Config()
        cfg.data_dir = d                       # fresh cache: pass 1 is truly cold
        cache, local = ActionCache(cfg.cache_path), LocalLLM(cfg)
        results = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for n in range(1, args.passes + 1):
                log = StepLog(cfg.log_path, run_id=f"pass{n}")
                for t in tasks:
                    page = browser.new_page()
                    r = run_task(page, cfg, cache, log, local, t)
                    r["pass"] = "cold" if n == 1 else f"warm{n - 1}"
                    results.append(r)
                    page.close()
            browser.close()
    out = {"model": cfg.local_model, "at": time.strftime("%Y-%m-%d"), "results": results}
    json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
    print(table(results))


def table(results):
    rows = ["| Pass | Engine said ok | Actually correct | Steps from cache | Claude tokens | Avg s/task |",
            "|---|---|---|---|---|---|"]
    for p in dict.fromkeys(r["pass"] for r in results):
        rs = [r for r in results if r["pass"] == p]
        steps = sum(r["steps"] for r in rs)
        cached = sum(r["tiers"].get("cache", 0) for r in rs)
        rows.append(f"| {p} | {sum(r['engine_ok'] for r in rs)}/{len(rs)} | {sum(r['correct'] for r in rs)}/{len(rs)}"
                    f" | {cached}/{steps} | 0 (screenshot agent ≈ {steps * DEFAULT_BASELINE_PER_STEP:,})"
                    f" | {sum(r['seconds'] for r in rs) / len(rs):.1f} |")
    return "\n".join(rows)


if __name__ == "__main__":
    main()
