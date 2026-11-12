"""Query + token usage log for MCP tool calls.

Every tool call Claude makes (run_browser_plan, get_page_elements, ...) is appended to
~/.browser_engine/calls.jsonl with:
  - what was asked: goal, start_url, step intents, query, ref ...
  - how long it took and whether it worked
  - est_claude_in / est_claude_out: tokens Claude spends sending the request and reading the
    result (chars / 4, the usual estimate for JSON/English text)
  - local_tokens / claude_api_tokens: tokens the engine itself spent during the call, taken from
    the step lines (steps.jsonl) written while the call ran

Env:
  BE_LOG_CALLS=0   turn the log off
  BE_LOG_VALUES=0  don't record typed values (names, emails, answers); intents are still logged
"""
import json
import os
import time
from collections import Counter, defaultdict

CHARS_PER_TOKEN = 4
MAX_TEXT = 160


def est_tokens(obj):
    if obj is None:
        return 0
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return max(1, len(s) // CHARS_PER_TOKEN)


def _short(v, n=MAX_TEXT):
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _log_values():
    return os.getenv("BE_LOG_VALUES", "1") != "0"


def describe_query(tool, args):
    """The human-readable part of a call: what was asked, without bulky payloads."""
    q = {}
    for k in ("goal", "start_url", "query", "intent", "action", "ref", "question", "op", "domain",
              "limit", "resume_url"):
        if args.get(k) not in (None, ""):
            q[k] = _short(args[k])
    for k in ("value", "option"):
        if args.get(k) is not None:
            q[k] = _short(args[k], 60) if _log_values() else "<hidden>"
    if args.get("confirm_irreversible"):
        q["confirm_irreversible"] = True
    steps = args.get("steps")
    if steps:
        q["n_steps"] = len(steps)
        q["steps"] = [
            _short(s.get("intent"), 90) + (
                f" = {_short(s['value'], 40) if _log_values() else '<hidden>'}" if s.get("value") is not None else "")
            for s in steps
        ]
    return q


def _engine_tokens(steps_log_path, offset):
    """Sum tokens from step lines appended to steps.jsonl after `offset` bytes."""
    local = claude = 0
    tiers = Counter()
    try:
        with open(steps_log_path, encoding="utf-8") as fh:
            fh.seek(offset)
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = r.get("tier")
                n = (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
                if t == "local":
                    local += n
                elif t in ("claude_text", "claude_vision", "planner"):
                    claude += n
                if r.get("kind") == "step" and t:
                    tiers[t] += 1
    except OSError:
        pass
    return local, claude, dict(tiers)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class CallLog:
    def __init__(self, path, steps_log_path):
        self.path = path
        self.steps_log_path = steps_log_path
        self.enabled = os.getenv("BE_LOG_CALLS", "1") != "0"

    def start(self):
        return time.time(), _size(self.steps_log_path)

    def finish(self, token, tool, args, result=None, error=None):
        if not self.enabled:
            return None
        t0, offset = token
        local, claude, tiers = _engine_tokens(self.steps_log_path, offset)
        ok = None
        if isinstance(result, dict):
            ok = result.get("ok", None if "error" not in result else False)
        rec = {
            "ts": round(t0, 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
            "tool": tool,
            "query": describe_query(tool, args),
            "ok": False if error else ok,
            "ms": int((time.time() - t0) * 1000),
            "est_claude_in": est_tokens(args),
            "est_claude_out": est_tokens(result if error is None else str(error)),
            "local_tokens": local,
            "claude_api_tokens": claude,
        }
        if tiers:
            rec["tiers"] = tiers
        if isinstance(result, dict):
            for k in ("needs_human", "blocked"):
                if result.get(k):
                    rec[k] = True
            if result.get("url"):
                rec["url"] = _short(result["url"], 120)
            if result.get("warning"):
                rec["partial_list"] = True
        if error is not None:
            rec["error"] = _short(error, 200)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a browser call
        return rec


def read_calls(path, since_ts=None):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_ts is None or r.get("ts", 0) >= since_ts:
                out.append(r)
    return out


def usage_summary(path, last=15, hours=None, tool=None):
    since = time.time() - hours * 3600 if hours else None
    calls = read_calls(path, since)
    if tool:
        calls = [c for c in calls if c.get("tool") == tool]
    if not calls:
        return {"calls": 0, "note": "no calls logged yet", "log_file": path}

    by_tool = defaultdict(lambda: {"calls": 0, "est_claude_tokens": 0, "local_tokens": 0, "ms": 0, "failed": 0})
    for c in calls:
        t = by_tool[c["tool"]]
        t["calls"] += 1
        t["est_claude_tokens"] += c.get("est_claude_in", 0) + c.get("est_claude_out", 0)
        t["local_tokens"] += c.get("local_tokens", 0)
        t["ms"] += c.get("ms", 0)
        if c.get("ok") is False:
            t["failed"] += 1

    est_in = sum(c.get("est_claude_in", 0) for c in calls)
    est_out = sum(c.get("est_claude_out", 0) for c in calls)
    heaviest = sorted(calls, key=lambda c: c.get("est_claude_out", 0), reverse=True)[:5]
    return {
        "calls": len(calls),
        "from": calls[0].get("time"),
        "to": calls[-1].get("time"),
        "est_claude_tokens": est_in + est_out,
        "est_claude_in": est_in,
        "est_claude_out": est_out,
        "local_tokens_free": sum(c.get("local_tokens", 0) for c in calls),
        "claude_api_tokens": sum(c.get("claude_api_tokens", 0) for c in calls),
        "captchas": sum(1 for c in calls if c.get("needs_human")),
        "failed_calls": sum(1 for c in calls if c.get("ok") is False),
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1]["est_claude_tokens"])),
        "heaviest_results": [
            {"time": c.get("time"), "tool": c["tool"], "est_tokens": c.get("est_claude_out", 0),
             "what": _what(c)} for c in heaviest
        ],
        "recent": [
            {"time": c.get("time"), "tool": c["tool"], "ok": c.get("ok"), "ms": c.get("ms"),
             "tokens": c.get("est_claude_in", 0) + c.get("est_claude_out", 0),
             "local": c.get("local_tokens", 0), "what": _what(c)}
            for c in calls[-last:]
        ],
        "log_file": path,
        "note": "est_* = chars/4 of the request and result Claude sees; local/claude_api = tokens "
                "the engine's own models used.",
    }


def _what(c):
    q = c.get("query", {})
    for k in ("goal", "intent", "query", "question", "op"):
        if q.get(k):
            s = q[k]
            if k == "goal" and q.get("n_steps"):
                s += f" ({q['n_steps']} steps)"
            return _short(s, 90)
    return q.get("start_url") or q.get("resume_url") or ""


def print_usage(s):
    if not s.get("calls"):
        print(f"No tool calls logged yet ({s.get('log_file')}).")
        return
    print(f"Tool calls: {s['calls']}   ({s['from']} → {s['to']})")
    print(f"Claude tokens (est):  {s['est_claude_tokens']:>9,}   request {s['est_claude_in']:,} / results {s['est_claude_out']:,}")
    print(f"Local model tokens:   {s['local_tokens_free']:>9,}   (free)")
    print(f"Claude API tokens:    {s['claude_api_tokens']:>9,}")
    print(f"Failed calls: {s['failed_calls']}   captchas: {s['captchas']}")
    print("\nBy tool:")
    print(f"  {'tool':<22}{'calls':>6}{'est tok':>10}{'avg':>7}{'local':>9}{'avg s':>7}{'fail':>6}")
    for name, t in s["by_tool"].items():
        avg = t["est_claude_tokens"] // max(t["calls"], 1)
        print(f"  {name:<22}{t['calls']:>6}{t['est_claude_tokens']:>10,}{avg:>7,}{t['local_tokens']:>9,}"
              f"{t['ms'] / 1000 / max(t['calls'], 1):>7.1f}{t['failed']:>6}")
    print("\nHeaviest results (trim these first):")
    for h in s["heaviest_results"]:
        print(f"  {h['est_tokens']:>6,}  {h['tool']:<20} {h['what']}")
    print("\nRecent calls:")
    for r in s["recent"]:
        mark = {True: "✓", False: "✗", None: "·"}[r["ok"]]
        print(f"  {r['time'][11:]} {mark} {r['tool']:<20} {r['tokens']:>6,} tok {r['local']:>6,} local "
              f"{r['ms'] / 1000:>5.1f}s  {r['what']}")
