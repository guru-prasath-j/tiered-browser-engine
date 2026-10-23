"""Reads steps.jsonl and compares actual Claude usage with a screenshot-loop baseline."""
import json
import os
from collections import Counter

from .engine import CLAUDE_TIERS

# Pure screenshot agent: ~1.5 calls per step (action + verify/retry) x ~10k input tokens
DEFAULT_BASELINE_PER_STEP = 15_000


def summarize(log_path, baseline_per_step=DEFAULT_BASELINE_PER_STEP, run_id=None):
    if not os.path.exists(log_path):
        return None
    with open(log_path, encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh if line.strip()]
    if run_id:
        recs = [r for r in recs if r.get("run_id") == run_id]

    steps = [r for r in recs if r["kind"] == "step" and r.get("ok")]
    failed = [r for r in recs if r["kind"] == "step" and not r.get("ok")]
    claude_in = sum(r.get("tokens_in", 0) for r in recs if r.get("tier") in CLAUDE_TIERS)
    claude_out = sum(r.get("tokens_out", 0) for r in recs if r.get("tier") in CLAUDE_TIERS)
    local = sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in recs if r.get("tier") == "local")
    baseline = len(steps) * baseline_per_step
    used = claude_in + claude_out

    # Which intents keep escalating to Claude? These are the next things to fix.
    escalations = Counter(
        r["intent"] for r in steps if r.get("tier") in ("claude_text", "claude_vision")
    )
    return {
        "runs": len({r["run_id"] for r in recs}),
        "steps_ok": len(steps),
        "steps_failed": len(failed),
        "by_tier": dict(Counter(r["tier"] for r in steps)),
        "claude_tokens": used,
        "claude_in": claude_in,
        "claude_out": claude_out,
        "local_tokens_free": local,
        "baseline_tokens": baseline,
        "saved_pct": round(100 * (1 - used / baseline), 1) if baseline else None,
        "top_escalations": escalations.most_common(5),
    }


def print_report(s):
    if not s:
        print("No runs logged yet.")
        return
    print(f"Runs: {s['runs']}   steps ok: {s['steps_ok']}   failed: {s['steps_failed']}")
    print("Steps solved by tier:")
    for tier in ("cache", "local", "claude_text", "claude_vision", "manual"):
        print(f"  {tier:<14} {s['by_tier'].get(tier, 0)}")
    print(f"Claude tokens used:     {s['claude_tokens']:>10,}  (in {s['claude_in']:,} / out {s['claude_out']:,})")
    print(f"Local tokens (free):    {s['local_tokens_free']:>10,}")
    print(f"Screenshot baseline:    {s['baseline_tokens']:>10,}  (estimate)")
    if s["saved_pct"] is not None:
        print(f"Saved vs baseline:      {s['saved_pct']}%")
    if s["top_escalations"]:
        print("Steps that keep reaching Claude (add to a skill or fix selectors):")
        for intent, n in s["top_escalations"]:
            print(f"  {n}x  {intent}")
