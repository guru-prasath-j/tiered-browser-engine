"""Visual logs: every run's steps, retries and failures on one local page, kept for 24 hours.

    browser-engine logs            # opens http://127.0.0.1:8772 (Browser Helper also serves /logs)

What it shows, per run: the goal, each step in order, and for each step the chain of tiers it
went through (rule -> cache -> match -> local -> claude ...), with the reason every failed attempt
gave. The step that broke a run is marked red with the page URL and a screenshot taken at that
moment, so you see exactly where and why it failed.

Retention: steps.jsonl, calls.jsonl and failure screenshots older than 24 hours are deleted
(BE_LOG_RETENTION_HOURS to change it). Pruning runs when the engine starts, at most every
10 minutes while it runs, and whenever the log page loads.
"""
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

PRUNE_EVERY_S = 600
ERR_DETAIL = re.compile(r"intercepts pointer events|not visible|not stable|not enabled|detached|"
                        r"outside of the viewport|not an? |strict mode violation|resolved to \d+ elements|"
                        r"waiting for|navigat|Target (page|closed)|net::", re.I)


def err_text(e, n=500):
    """An error's first line plus the lines that say WHY (e.g. '<div class=modal> intercepts pointer
    events'), which Playwright puts further down and a first-line cut used to drop."""
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
    if not lines:
        return type(e).__name__
    keep, seen = [lines[0]], {lines[0]}
    for ln in lines[1:]:
        ln = ln.lstrip("- ").strip()
        if re.match(r"^\d+ × ", ln) or ln.startswith("waiting for element to be"):
            continue  # Playwright's retry counters: noise
        if ERR_DETAIL.search(ln) and ln not in seen:
            keep.append(ln)
            seen.add(ln)
    out = " | ".join(keep)
    return out if len(out) <= n else out[: n - 1] + "…"


def log_value(intent, value):
    """The typed value as it may appear in logs: hidden for secrets or with BE_LOG_VALUES=0."""
    if value is None:
        return None
    from .safety import CARD_LIKE, SENSITIVE

    if os.getenv("BE_LOG_VALUES", "1") == "0":
        return "<hidden>"
    if SENSITIVE.search(str(intent or "")) or CARD_LIKE.match(str(value)):
        return "<secret>"
    s = str(value)
    return s if len(s) <= 120 else s[:119] + "…"


def engine_version():
    try:
        from importlib.metadata import version

        return version("tiered-browser-engine")
    except Exception:
        return "dev"
SHOT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.jpg$")
DEFAULT_PORT = 8772

_last_prune = 0.0
_prune_lock = threading.Lock()


def retention_s():
    try:
        hours = float(os.getenv("BE_LOG_RETENTION_HOURS", "24"))
    except ValueError:
        hours = 24.0
    return max(0.1, hours) * 3600


def shots_dir(cfg):
    return os.path.join(cfg.data_dir, "shots")


@contextmanager
def file_lock(path):
    """Cross-process lock so a prune never drops lines another process is appending."""
    fh = open(path + ".lock", "a")
    try:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:  # Windows: best effort
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_UN)
        except ImportError:
            pass
        fh.close()


def append_jsonl(path, rec):
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    with file_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)


def _prune_jsonl(path, cutoff):
    if not os.path.exists(path):
        return 0
    with file_lock(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        keep = []
        for line in lines:
            if not line.strip():
                continue
            try:
                ts = float(json.loads(line).get("ts", 0))
            except (ValueError, AttributeError, TypeError):
                continue  # broken line: drop it
            if ts >= cutoff:
                keep.append(line)
        dropped = len(lines) - len(keep)
        if dropped:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
            os.replace(tmp, path)
        return dropped


def prune(cfg, now=None):
    """Delete log lines and screenshots older than the retention window. Returns counts."""
    global _last_prune
    now = now or time.time()
    cutoff = now - retention_s()
    out = {"steps": 0, "calls": 0, "shots": 0}
    with _prune_lock:
        out["steps"] = _prune_jsonl(cfg.log_path, cutoff)
        out["calls"] = _prune_jsonl(cfg.calls_path, cutoff)
        d = shots_dir(cfg)
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        out["shots"] += 1
                except OSError:
                    pass
        _last_prune = now
    return out


def maybe_prune(cfg):
    if time.time() - _last_prune >= PRUNE_EVERY_S:
        try:
            prune(cfg)
        except Exception as e:  # logging must never break a run
            print(f"[browser-engine] log prune failed: {e}", file=sys.stderr)


def save_failure_shot(page, cfg, run_id, label="fail"):
    """Viewport JPEG of the page at the moment a step failed. Returns the file name or None."""
    try:
        d = shots_dir(cfg)
        os.makedirs(d, exist_ok=True)
        name = f"{run_id}_{label}_{int(time.time() * 1000) % 10**8}.jpg"
        page.screenshot(path=os.path.join(d, name), type="jpeg", quality=55, full_page=False,
                        timeout=5000)
        return name
    except Exception:
        return None


# ---- reading -------------------------------------------------------------------
def _read(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


ATTEMPT_KEYS = ("ts", "dt_ms", "tier", "ok", "action", "note", "tokens_in", "tokens_out", "layer", "model",
                "raw", "ref", "target", "selector", "seen")
STALE_RUN_S = 600  # a run with no end line and no activity for 10 min was interrupted (crash, kill)


def _new_step(r, attempts, **kw):
    fails = sum(1 for a in attempts if a.get("ok") is False)
    step = {"ts": r["ts"], "intent": r.get("intent"), "ok": bool(r.get("ok")), "tier": r.get("tier"),
            "ms": r.get("ms"), "url": r.get("url"), "note": r.get("note"), "value": r.get("value"),
            "attempts": attempts, "retried": fails > 0 and bool(r.get("ok")), "shot": None}
    for k in ("action", "ref", "target", "blocked"):
        if r.get(k) is not None:
            step[k] = r[k]
    step.update(kw)
    return step


def build_runs(cfg, now=None):
    """Group steps.jsonl into runs -> steps -> attempts, newest run first. Also returns the
    session lines (engine version / models per process) separately."""
    now = now or time.time()
    keep_s = retention_s()
    cutoff = now - keep_s
    runs, order, sessions = {}, [], []
    for r in _read(cfg.log_path):
        if float(r.get("ts", 0)) < cutoff:
            continue
        kind = r.get("kind")
        if kind == "session":
            sessions.append({k: v for k, v in r.items() if k not in ("run_id", "kind")})
            continue
        rid = r.get("run_id") or "?"
        run = runs.get(rid)
        if run is None:
            run = runs[rid] = {"run_id": rid, "start": r["ts"], "end": r["ts"], "goal": None,
                               "status": "running", "steps": [], "pending": [], "events": [],
                               "plan": None, "planned": None, "start_url": None, "tiers": {},
                               "claude_tokens": 0, "local_tokens": 0, "error": None}
            order.append(rid)
        run["end"] = r["ts"]
        if kind == "attempt":
            run["pending"].append({k: r.get(k) for k in ATTEMPT_KEYS if r.get(k) is not None})
            tok = (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
            if r.get("tier") in ("local", "vision_local"):
                run["local_tokens"] += tok
            elif r.get("tier") in ("planner", "claude_text", "claude_vision"):
                run["claude_tokens"] += tok
        elif kind == "step":
            attempts = run["pending"]
            run["pending"] = []
            run["steps"].append(_new_step(r, attempts))
        elif kind == "shot":
            target = run["steps"][-1] if run["steps"] else None
            if target is None or target["ok"]:
                target = _new_step(dict(r, ok=False, tier=None), run["pending"])
                run["pending"] = []
                run["steps"].append(target)
            target["shot"] = r.get("file")
            target["url"] = target.get("url") or r.get("url")
            for k in ("error", "reason", "elements", "n_elements"):
                if r.get(k) is not None:
                    target[k] = r[k]
        elif kind == "run_start":
            run["goal"] = r.get("goal")
            run["planned"] = r.get("plan")
            run["start_url"] = r.get("start_url")
        elif kind == "run":
            run["goal"] = r.get("goal") or run["goal"]
            run["tiers"] = r.get("tiers") or run["tiers"]
            if r.get("ok"):
                run["status"] = "ok"
            elif r.get("crashed"):
                run["status"] = "crashed"
            elif r.get("needs_human"):
                run["status"] = "needs_human"
            elif r.get("blocked"):
                run["status"] = "blocked"
            else:
                run["status"] = "failed"
        elif kind == "error":
            run["error"] = {k: r.get(k) for k in ("error", "type", "trace")}
        elif kind == "layer":
            run["pending"].append({"ts": r["ts"], "tier": "layer2", "ok": None, "note": r.get("note")})
        elif kind == "plan":
            run["plan"] = r.get("tier")
        else:  # nav, popup, human, human_wait and anything future: shown as a timeline event
            ev = {k: v for k, v in r.items() if k not in ("run_id", "intent") and v is not None}
            if r.get("intent"):
                ev["intent"] = r["intent"]
            run["events"].append(ev)

    out = []
    for rid in reversed(order):
        run = runs[rid]
        if run["pending"]:  # attempts with no closing step: the run stopped mid-step
            run["steps"].append({"ts": run["end"], "intent": None, "ok": False, "tier": None,
                                 "ms": None, "url": None, "note": None, "attempts": run["pending"],
                                 "retried": False, "shot": None, "unfinished": True})
        del run["pending"]
        if run["status"] == "running":
            if run["steps"] and all(s["ok"] for s in run["steps"]) and not run["goal"]:
                run["status"] = "ok"  # a single manual step (do_browser_step) logs no run line
            elif not run["steps"] and not run["planned"] and run["events"]:
                run["status"] = "event"  # e.g. a wait_for_human call on its own
            elif now - run["end"] > STALE_RUN_S:
                run["status"] = "interrupted"  # started, never finished: crash or killed process
            elif any(not s["ok"] for s in run["steps"]) and not run["planned"]:
                run["status"] = "failed"
        if not run["goal"]:
            first = next((s["intent"] for s in run["steps"] if s.get("intent")), None)
            if first:
                run["goal"] = f"manual step: {first}"
            elif run["events"]:
                run["goal"] = run["events"][0].get("kind", "event").replace("_", " ")
            else:
                run["goal"] = "(no goal)"
            run["manual"] = True
        run["expires_at"] = run["end"] + keep_s
        run["ms"] = int((run["end"] - run["start"]) * 1000)
        run["retries"] = sum(1 for s in run["steps"] if s.get("retried"))
        run["failed_steps"] = sum(1 for s in run["steps"] if not s["ok"])
        out.append(run)
    return out, sessions


def summary(runs):
    steps = [s for r in runs for s in r["steps"]]
    tiers = {}
    for s in steps:
        if s["ok"] and s.get("tier"):
            tiers[s["tier"]] = tiers.get(s["tier"], 0) + 1
    by_status = {}
    for r in runs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    fail_spots = {}
    for r in runs:
        for s in r["steps"]:
            if not s["ok"] and s.get("intent"):
                fail_spots[s["intent"]] = fail_spots.get(s["intent"], 0) + 1
    # Where the time goes: total and average ms per tier across all attempts (model latency,
    # timeouts), and the slowest steps overall.
    tier_ms = {}
    for s in steps:
        for a in s["attempts"]:
            if a.get("dt_ms") is not None and a.get("tier") not in (None, "layer2"):
                t = tier_ms.setdefault(a["tier"], [0, 0])
                t[0] += a["dt_ms"]
                t[1] += 1
    slow = sorted((s for s in steps if s.get("ms")), key=lambda s: -s["ms"])[:5]
    return {"runs": len(runs), "by_status": by_status, "steps": len(steps),
            "steps_failed": sum(1 for s in steps if not s["ok"]),
            "steps_retried": sum(1 for s in steps if s.get("retried")),
            "steps_layer2": sum(1 for s in steps if s["ok"] and str(s.get("tier") or "").startswith("vision")),
            "tiers": tiers,
            "time_by_tier": {k: {"total_ms": v[0], "calls": v[1], "avg_ms": int(v[0] / v[1])}
                             for k, v in sorted(tier_ms.items(), key=lambda kv: -kv[1][0])},
            "slowest_steps": [{"intent": s.get("intent"), "ms": s["ms"], "tier": s.get("tier"),
                               "ok": s["ok"]} for s in slow],
            "top_failures": sorted(fail_spots.items(), key=lambda kv: -kv[1])[:5],
            "retention_hours": round(retention_s() / 3600, 2)}


def payload(cfg):
    prune(cfg)
    runs, sessions = build_runs(cfg)
    return {"now": time.time(), "summary": summary(runs), "runs": runs, "sessions": sessions[-5:][::-1]}


def shot_path(cfg, name):
    if not name or not SHOT_RE.match(name):
        return None
    p = os.path.join(shots_dir(cfg), name)
    return p if os.path.isfile(p) else None


# ---- local server (stdlib, no extra deps) ---------------------------------------------
def make_handler(cfg, port):
    from http.server import BaseHTTPRequestHandler

    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host", "") not in allowed:  # DNS rebinding guard
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path in ("/", "/logs"):
                return self._send(200, PAGE.replace("__API__", "/api/logs").replace(
                    "__SHOTS__", "/shots/").encode(), "text/html; charset=utf-8")
            if path == "/api/logs":
                body = json.dumps(payload(cfg), default=str).encode()
                return self._send(200, body, "application/json")
            if path.startswith("/shots/"):
                p = shot_path(cfg, path[len("/shots/"):])
                if p:
                    with open(p, "rb") as fh:
                        return self._send(200, fh.read(), "image/jpeg")
            return self._send(404, b"not found", "text/plain")

    return H


def serve(cfg, port=DEFAULT_PORT, open_browser=True):
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, port))
    url = f"http://127.0.0.1:{port}/"
    print(f"Log viewer: {url}  (last {round(retention_s() / 3600)}h; Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Browser Engine Logs</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e4e2dc;--ok:#1f7a55;--okbg:#e3f3ea;
--bad:#b3261e;--badbg:#fbe7e5;--warn:#9a6200;--warnbg:#fdf1d8;--info:#2b5ea7;--infobg:#e6eefa;--chip:#efede8}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--card:#1f1f1e;--ink:#ecebe7;--muted:#a09f99;--line:#32312e;
--ok:#5cc29c;--okbg:#17332a;--bad:#f2867c;--badbg:#3a1d1a;--warn:#e7b65a;--warnbg:#382c14;--info:#86aef0;--infobg:#1b2740;--chip:#2a2a28}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0}.muted{color:var(--muted)}.sp{flex:1}
.wrap{max-width:1100px;margin:0 auto;padding:14px 16px 40px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.tile span{color:var(--muted);font-size:12px}
.bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.fbtn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:4px 11px;font:inherit;cursor:pointer}
.fbtn.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
input[type=search]{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 9px;font:inherit;min-width:180px}
.hot{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:14px}
.hot ol{margin:6px 0 0;padding-left:20px}
.run{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
.run>summary{list-style:none;cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.run>summary::-webkit-details-marker{display:none}
.goal{font-weight:600;flex:1;min-width:200px;word-break:break-word}
.st{font-size:12px;font-weight:600;border-radius:999px;padding:2px 9px;white-space:nowrap}
.st.ok{background:var(--okbg);color:var(--ok)}.st.failed{background:var(--badbg);color:var(--bad)}
.st.needs_human,.st.blocked{background:var(--warnbg);color:var(--warn)}.st.running{background:var(--infobg);color:var(--info)}
.meta{font-size:12px;color:var(--muted);white-space:nowrap}
.steps{border-top:1px solid var(--line);padding:6px 12px 12px}
.step{display:grid;grid-template-columns:22px 1fr;gap:8px;padding:8px 0;border-bottom:1px dashed var(--line)}
.step:last-child{border-bottom:0}
.dot{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;margin-top:1px}
.dot.ok{background:var(--okbg);color:var(--ok)}.dot.bad{background:var(--bad);color:#fff}.dot.retry{background:var(--warnbg);color:var(--warn)}
.intent{font-weight:600;word-break:break-word}.intent small{font-weight:400;color:var(--muted)}
.chain{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin-top:5px}
.a{font-size:12px;border-radius:6px;padding:1px 7px;background:var(--chip);border:1px solid transparent;cursor:default}
.a.ok{background:var(--okbg);color:var(--ok)}.a.bad{background:var(--badbg);color:var(--bad)}
.arrow{color:var(--muted);font-size:11px}
.lay{font-size:11px;font-weight:600;letter-spacing:.02em;color:var(--info);background:var(--infobg);border-radius:6px;padding:1px 7px}
.why{margin-top:6px;font-size:12.5px;color:var(--muted)}
.why li{margin:2px 0;word-break:break-word}.why b{color:var(--ink);font-weight:600}
.failbox{margin-top:8px;border:1px solid var(--bad);background:var(--badbg);border-radius:8px;padding:8px 10px}
.failbox .u{font-size:12px;word-break:break-all}
.failbox img{display:block;max-width:100%;max-height:360px;margin-top:8px;border-radius:6px;border:1px solid var(--line);cursor:zoom-in}
.step.bad .intent{color:var(--bad)}
.empty{text-align:center;color:var(--muted);padding:50px 10px}
.exp{font-size:12px;color:var(--muted)}
dialog{border:0;padding:0;background:transparent;max-width:96vw}dialog img{max-width:96vw;max-height:92vh;border-radius:8px}
dialog::backdrop{background:rgba(0,0,0,.75)}
.sess{font-size:12px;margin-bottom:10px;word-break:break-word}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px;margin-bottom:14px}.grid2 .hot{margin:0}
.st.crashed,.st.interrupted{background:var(--bad);color:#fff}.st.event{background:var(--chip);color:var(--muted)}
.ev{font-size:12px;color:var(--muted);padding:3px 0;word-break:break-word}.ev b{color:var(--ink);font-weight:600}
.raw{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);border-radius:6px;padding:4px 6px;margin-top:3px;white-space:pre-wrap;word-break:break-all;color:var(--ink)}
.els{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;margin:6px 0 0;padding-left:16px;color:var(--ink)}
details.more>summary{cursor:pointer;font-size:12px;color:var(--muted);margin-top:6px}
.trace{font:11px/1.35 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;max-height:260px;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px;margin-top:6px}
.plan{font-size:12px;color:var(--muted);margin:2px 0 6px;word-break:break-word}
.seen{font-size:11px;margin-left:4px}
</style></head><body>
<header><h1>Browser Engine Logs</h1><span class="muted" id="ret"></span><span class="sp"></span>
<label class="muted"><input type="checkbox" id="auto" checked> auto-refresh</label>
<button class="fbtn" id="reload">Refresh</button></header>
<div class="wrap">
  <div class="sess muted" id="sess"></div>
  <div class="tiles" id="tiles"></div>
  <div class="grid2"><div class="hot" id="hot" hidden></div><div class="hot" id="slow" hidden></div></div>
  <div class="bar" style="margin-bottom:12px">
    <button class="fbtn on" data-f="all">All</button><button class="fbtn" data-f="failed">Failed</button>
    <button class="fbtn" data-f="retried">Retried</button><button class="fbtn" data-f="human">Needs you</button><button class="fbtn" data-f="crash">Crashed / interrupted</button>
    <span class="sp"></span><input type="search" id="q" placeholder="Filter by goal or step">
  </div>
  <div id="runs"></div>
</div>
<dialog id="dlg"><img id="big" alt=""></dialog>
<script>
const API='__API__',SHOTS='__SHOTS__';let data=null,filter='all',open=new Set();
const TIER={rule:'rule',cache:'cache',match:'label match',local:'local LLM',claude_text:'Claude text',claude_vision:'Claude vision',manual:'manual (agent)',planner:'planner',dom:'read page',vision_local:'📷 local vision'};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t=ts=>new Date(ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
const ago=s=>s<60?Math.round(s)+'s':s<3600?Math.round(s/60)+'m':(s/3600).toFixed(1)+'h';
const dur=ms=>ms==null?'':ms<1000?ms+' ms':(ms/1000).toFixed(ms<10000?1:0)+' s';
const shot=n=>SHOTS+encodeURIComponent(n);
const su=u=>{u=String(u??'');return u.length>140?u.slice(0,139)+'…':u};
function left(r){const s=r.expires_at-data.now;return s<=0?'deleting soon':'deleted in '+ago(s)}
function sess(){const s=(data.sessions||[])[0];const el=document.getElementById('sess');
 if(!s){el.textContent='';return}
 el.innerHTML=`Engine <b>${esc(s.version)}</b> · ${esc(s.mode)} · local model ${esc(s.local_model)} ${s.local_up?'(up)':'<b style="color:var(--bad)">(down)</b>'} · screenshot layer: ${s.vision_models&&s.vision_models.length?esc(s.vision_models.join(', ')):'off'} · Claude API ${s.claude_api?'on':'off'} · ${esc(s.os)} · started ${t(s.ts)}`}
function tiles(){const s=data.summary,st=s.by_status;
 const ok=st.ok||0, nruns=s.runs-(st.event||0), rate=nruns?Math.round(100*ok/nruns)+'%':'–';
 const top=Object.entries(s.tiers).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${TIER[k]||k} ${v}`).join(' · ')||'–';
 document.getElementById('tiles').innerHTML=[
  [nruns,'runs (last '+s.retention_hours+'h)'],[rate,'runs succeeded'],[(st.failed||0)+(st.blocked||0),'runs failed / blocked'],
  [(st.crashed||0)+(st.interrupted||0),'crashed / interrupted'],[st.needs_human||0,'stopped for a bot check'],[s.steps_failed,'steps failed'],
  [s.steps_retried,'steps that needed a retry'],[s.steps_layer2||0,'steps solved from screenshots']]
  .map(([v,l])=>`<div class="tile"><b>${v}</b><span>${l}</span></div>`).join('')+
  `<div class="tile" style="grid-column:1/-1"><span>Steps solved by tier</span><div style="margin-top:3px">${esc(top)}</div></div>`;
 const hot=document.getElementById('hot');
 if(s.top_failures.length){hot.hidden=false;hot.innerHTML='<b>Where failures happen most</b><ol>'+s.top_failures.map(([i,n])=>`<li>${esc(i)} <span class="muted">×${n}</span></li>`).join('')+'</ol>'}
 else hot.hidden=true;
 const slow=document.getElementById('slow');const tt=Object.entries(s.time_by_tier||{});
 if(tt.length||s.slowest_steps.length){slow.hidden=false;
  slow.innerHTML='<b>Where the time goes</b><ol>'+tt.map(([k,v])=>`<li>${esc(TIER[k]||k)}: ${dur(v.total_ms)} total <span class="muted">(${v.calls} tries, avg ${dur(v.avg_ms)})</span></li>`).join('')+'</ol>'+
  (s.slowest_steps.length?'<b>Slowest steps</b><ol>'+s.slowest_steps.map(x=>`<li>${esc(x.intent)} <span class="muted">${dur(x.ms)}${x.ok?'':' · failed'}</span></li>`).join('')+'</ol>':'')}
 else slow.hidden=true;
 document.getElementById('ret').textContent='logs older than '+s.retention_hours+'h are deleted automatically'}
function chain(st){if(!st.attempts.length)return st.tier?`<span class="a ok">${esc(TIER[st.tier]||st.tier)}</span>`:'';
 return st.attempts.map(a=>{if(a.tier==='layer2')return '<span class="lay" title="'+esc(a.note||'')+'">layer 2: screenshots</span>';
  const tok=(a.tokens_in||0)+(a.tokens_out||0);
  const tip=[a.model,a.action&&(a.action+(a.ref?' '+a.ref:'')),a.target,a.selector,a.note].filter(Boolean).join(' · ');
  return `<span class="a ${a.ok?'ok':'bad'}" title="${esc(tip)}">${a.ok?'✓':'✗'} ${esc(TIER[a.tier]||a.tier)}${a.dt_ms!=null?' · '+dur(a.dt_ms):''}${tok?` · ${tok} tok`:''}</span>`}).join('<span class="arrow">→</span>')}
function why(st){const fails=st.attempts.filter(a=>a.ok===false&&(a.note||a.raw));if(!fails.length||!(!st.ok||st.retried))return '';
 return '<ul class="why">'+fails.map(a=>`<li><b>${esc(TIER[a.tier]||a.tier)}</b>${a.model?' <span class="muted">('+esc(a.model)+')</span>':''}${a.action?' '+esc(a.action):''}${a.target?' → '+esc(a.target):a.ref?' → '+esc(a.ref):''}: ${esc(a.note||'')}${a.dt_ms!=null?' <span class="muted">· '+dur(a.dt_ms)+'</span>':''}${a.seen?` <a class="seen" href="${shot(a.seen)}" target="_blank">what the model saw</a>`:''}${a.raw?'<div class="raw">model replied: '+esc(a.raw)+'</div>':''}</li>`).join('')+'</ul>'}
function step(st,i){const bad=!st.ok, cls=bad?'bad':st.retried?'retry':'ok';
 let box='';if(bad){box=`<div class="failbox"><b>${st.unfinished?'Stopped during this step':st.blocked?'Blocked (needs approval)':'Failed here'}</b>${st.reason?' — '+esc(st.reason):''}${st.error?'<div>'+esc(st.error)+'</div>':st.note?'<div>'+esc(st.note)+'</div>':''}${st.url?`<div class="u" title="${esc(st.url)}">${esc(su(st.url))}</div>`:''}${st.shot?`<img loading="lazy" src="${shot(st.shot)}" alt="page when it failed">`:''}${st.elements&&st.elements.length?`<details class="more"><summary>Elements the engine saw near this step (${st.n_elements??'?'} on page)</summary><ul class="els">${st.elements.map(e=>'<li>'+esc(e)+'</li>').join('')}</ul></details>`:''}</div>`}
 const label=st.intent?esc(st.intent):'<i>unknown step</i>';
 const extra=[st.value!=null?'value “'+esc(st.value)+'”':'',st.action&&st.tier==='manual'?esc(st.action)+(st.target?' → '+esc(st.target):''):''].filter(Boolean).join(' · ');
 return `<div class="step ${bad?'bad':''}"><div class="dot ${cls}">${bad?'!':st.retried?'↻':'✓'}</div><div>
  <div class="intent">${i+1}. ${label} <small>${t(st.ts)}${st.ms!=null?' · '+dur(st.ms):''}${st.retried?' · retried':''}${extra?' · '+extra:''}</small></div>
  <div class="chain">${chain(st)}</div>${why(st)}${box}</div></div>`}
function evLine(e){const k=e.kind;let txt='';
 if(k==='nav')txt=`<b>opened</b> <span title="${esc(e.url)}">${esc(su(e.url))}</span>${e.final_url?' → '+esc(su(e.final_url)):''}${e.status?' · HTTP '+e.status:''} · ${dur(e.ms)}${e.ok===false?' · <b style="color:var(--bad)">failed: '+esc(e.error)+'</b>':''}`;
 else if(k==='popup')txt=`<b>closed popup</b> ${esc((e.closed||[]).join(', '))}${e.intent?' before “'+esc(e.intent)+'”':''}`;
 else if(k==='human')txt=`<b>bot check</b> ${esc(e.note||'')}`;
 else if(k==='human_wait')txt=`<b>waited for you</b> (${esc(e.challenge)}) · ${e.resolved?'solved':'not solved yet'} · ${dur(e.ms)}`;
 else txt=`<b>${esc(k)}</b> ${esc(e.note||e.error||'')}`;
 return `<div class="ev">${t(e.ts)} · ${txt}</div>`}
function runHtml(r){const st={ok:'OK',failed:'Failed',needs_human:'Bot check',blocked:'Blocked (needs approval)',running:'Running',crashed:'Crashed',interrupted:'Interrupted',event:'Event'}[r.status]||r.status;
 const meta=[t(r.start),dur(r.ms),r.steps.length+' step'+(r.steps.length==1?'':'s'),r.retries?r.retries+' retried':'',r.claude_tokens?r.claude_tokens+' Claude tok':'',r.local_tokens?r.local_tokens+' local tok':''].filter(Boolean).join(' · ');
 const plan=r.planned&&r.planned.length?`<div class="plan">Plan: ${r.planned.map((p,i)=>esc((i+1)+'. '+p.intent+(p.value!=null?' = '+p.value:''))).join(' · ')}${r.start_url?' · start '+esc(su(r.start_url)):''}</div>`:'';
 const err=r.error?`<div class="failbox"><b>Crashed: ${esc(r.error.type)}</b><div>${esc(r.error.error)}</div><details class="more"><summary>traceback</summary><div class="trace">${esc(r.error.trace)}</div></details></div>`:'';
 return `<details class="run" data-id="${esc(r.run_id)}" ${open.has(r.run_id)?'open':''}><summary>
  <span class="st ${esc(r.status)}">${st}</span><span class="goal">${esc(r.goal)}</span>
  <span class="meta">${meta}</span><span class="exp">${left(r)}</span></summary>
  <div class="steps">${plan}${(r.events||[]).map(evLine).join('')}${r.steps.length?r.steps.map(step).join(''):(r.events||[]).length?'':'<div class="muted">No steps logged.</div>'}${err}</div></details>`}
function render(){if(!data)return;sess();tiles();const q=document.getElementById('q').value.toLowerCase();
 let runs=data.runs.filter(r=>filter==='all'||(filter==='failed'&&(r.status==='failed'||r.status==='blocked'))||(filter==='retried'&&r.retries>0)||(filter==='human'&&r.status==='needs_human')||(filter==='crash'&&(r.status==='crashed'||r.status==='interrupted')));
 if(q)runs=runs.filter(r=>(r.goal||'').toLowerCase().includes(q)||r.steps.some(s=>(s.intent||'').toLowerCase().includes(q)));
 const el=document.getElementById('runs');
 el.innerHTML=runs.length?runs.map(runHtml).join(''):`<div class="empty">${data.runs.length?'No runs match this filter.':'No runs in the last '+data.summary.retention_hours+' hours.'}</div>`;
 el.querySelectorAll('details.run').forEach(d=>{d.addEventListener('toggle',()=>{d.open?open.add(d.dataset.id):open.delete(d.dataset.id)})});
 el.querySelectorAll('.failbox img').forEach(im=>im.onclick=()=>{document.getElementById('big').src=im.src;document.getElementById('dlg').showModal()})}
async function load(){try{const r=await fetch(API,{cache:'no-store'});data=await r.json();
 if(!open.size&&data.runs.length&&!['ok','event'].includes(data.runs[0].status))open.add(data.runs[0].run_id);render()}catch(e){document.getElementById('runs').innerHTML='<div class="empty">Could not load logs.</div>'}}
document.querySelectorAll('[data-f]').forEach(b=>b.onclick=()=>{filter=b.dataset.f;document.querySelectorAll('[data-f]').forEach(x=>x.classList.toggle('on',x===b));render()});
document.getElementById('q').oninput=render;document.getElementById('reload').onclick=load;
document.getElementById('dlg').onclick=e=>e.target.closest('dialog').close();
setInterval(()=>{if(document.getElementById('auto').checked&&!document.hidden)load()},10000);load();
</script></body></html>"""
