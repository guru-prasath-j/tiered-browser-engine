"""Call log tests. Run: python tests/test_calllog.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.calllog import CallLog, print_usage, read_calls, usage_summary  # noqa: E402


def test_logs_query_tokens_and_engine_usage():
    d = tempfile.mkdtemp()
    calls, steps = os.path.join(d, "calls.jsonl"), os.path.join(d, "steps.jsonl")
    with open(steps, "w") as fh:
        fh.write(json.dumps({"kind": "step", "tier": "local", "tokens_in": 999, "tokens_out": 1}) + "\n")
    log = CallLog(calls, steps)

    tok = log.start()
    # Steps written during the call are attributed to it; the older line is not.
    with open(steps, "a") as fh:
        fh.write(json.dumps({"kind": "step", "tier": "local", "tokens_in": 800, "tokens_out": 20}) + "\n")
        fh.write(json.dumps({"kind": "step", "tier": "cache"}) + "\n")
    args = {"goal": "cheapest flight", "start_url": "https://x.test",
            "steps": [{"intent": "type into the email box", "value": "me@example.com"}, {"intent": "read price"}]}
    rec = log.finish(tok, "run_browser_plan", args, result={"ok": True, "extracted": ["₹15,028"] * 50})
    assert rec["local_tokens"] == 820 and rec["tiers"] == {"local": 1, "cache": 1}, rec
    assert rec["query"]["n_steps"] == 2 and "me@example.com" in rec["query"]["steps"][0]
    assert rec["est_claude_out"] > 50 and rec["ok"] is True

    tok = log.start()
    log.finish(tok, "get_page_elements", {"query": "Cheapest", "limit": 5}, error=TimeoutError("slow"))

    os.environ["BE_LOG_VALUES"] = "0"
    tok = log.start()
    r3 = log.finish(tok, "do_browser_step", {"intent": "type into the email box", "action": "type",
                                             "value": "secret@x.com"}, result={"ok": True})
    del os.environ["BE_LOG_VALUES"]
    assert r3["query"]["value"] == "<hidden>"

    assert len(read_calls(calls)) == 3
    s = usage_summary(calls, last=5)
    assert s["calls"] == 3 and s["failed_calls"] == 1 and s["local_tokens_free"] == 820
    assert set(s["by_tool"]) == {"run_browser_plan", "get_page_elements", "do_browser_step"}
    assert usage_summary(calls, tool="get_page_elements")["calls"] == 1
    print_usage(s)


def test_disabled_and_empty():
    d = tempfile.mkdtemp()
    os.environ["BE_LOG_CALLS"] = "0"
    log = CallLog(os.path.join(d, "c.jsonl"), os.path.join(d, "s.jsonl"))
    del os.environ["BE_LOG_CALLS"]
    assert log.finish(log.start(), "browser_status", {}, result={}) is None
    assert usage_summary(os.path.join(d, "c.jsonl"))["calls"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
