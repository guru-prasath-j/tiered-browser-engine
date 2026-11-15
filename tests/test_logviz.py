"""Visual logs: 24h retention, runs/steps/attempts grouping, local viewer server.

    python tests/test_logviz.py     (no browser or LLM needed)
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_engine import logviz  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.engine import StepLog  # noqa: E402


def _cfg():
    os.environ["BE_DATA_DIR"] = tempfile.mkdtemp()
    os.environ.pop("BE_LOG_RETENTION_HOURS", None)
    cfg = Config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    return cfg


def _write(path, recs):
    with open(path, "a", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def test_prune_24h():
    cfg = _cfg()
    now = time.time()
    old, new = now - 25 * 3600, now - 23 * 3600
    _write(cfg.log_path, [{"ts": old, "run_id": "a", "kind": "step"}, {"ts": new, "run_id": "b", "kind": "step"}])
    with open(cfg.log_path, "a") as fh:
        fh.write("not json\n")
    _write(cfg.calls_path, [{"ts": old, "tool": "x"}, {"ts": new, "tool": "y"}])
    d = logviz.shots_dir(cfg)
    os.makedirs(d)
    for name, ts in (("old.jpg", old), ("new.jpg", new)):
        p = os.path.join(d, name)
        open(p, "wb").write(b"\xff\xd8")
        os.utime(p, (ts, ts))
    out = logviz.prune(cfg, now)
    assert out == {"steps": 2, "calls": 1, "shots": 1}, out
    assert [json.loads(line)["run_id"] for line in open(cfg.log_path)] == ["b"]
    assert [json.loads(line)["tool"] for line in open(cfg.calls_path)] == ["y"]
    assert os.listdir(d) == ["new.jpg"]
    assert logviz.prune(cfg, now) == {"steps": 0, "calls": 0, "shots": 0}  # idempotent
    os.environ["BE_LOG_RETENTION_HOURS"] = "1"
    assert logviz.prune(cfg, now)["steps"] == 1  # shorter window
    os.environ.pop("BE_LOG_RETENTION_HOURS")


def test_build_runs():
    cfg = _cfg()
    # run 1: step 1 retried (cache fails, local works), step 2 fails everywhere -> shot
    log = StepLog(cfg.log_path, run_id="r1")
    log.write("attempt", intent="click search", tier="cache", ok=False, note="selector gone")
    log.write("attempt", intent="click search", tier="local", ok=True, action="click", note="ok",
              tokens_in=300, tokens_out=20)
    log.write("step", intent="click search", tier="local", ok=True, ms=900, url="https://x.test/")
    log.write("attempt", intent="click sort by price", tier="match", ok=False, note="2 elements match")
    log.write("attempt", intent="click sort by price", tier="local", ok=False, note="not JSON")
    log.write("step", intent="click sort by price", tier=None, ok=False, ms=2000, url="https://x.test/s")
    log.write("shot", file="r1_fail_1.jpg", intent="click sort by price", url="https://x.test/s",
              reason="StepFailed", error="could not complete step")
    log.write("run", ok=False, goal="cheap earbuds", completed=1, tiers={"local": 1})
    # run 2: clean
    log2 = StepLog(cfg.log_path, run_id="r2")
    log2.write("attempt", intent="press enter", tier="cache", ok=True)
    log2.write("step", intent="press enter", tier="cache", ok=True, ms=40)
    log2.write("run", ok=True, goal="search", steps=1)
    # run 3: manual step (do_browser_step), no run line
    StepLog(cfg.log_path, run_id="r3").write("step", intent="click apply", tier="manual", ok=True)
    # run 4: bot check before any step closed
    log4 = StepLog(cfg.log_path, run_id="r4")
    log4.write("shot", file=None, intent="open results", reason="bot check", error="captcha")
    log4.write("run", ok=False, goal="flights", needs_human=True)

    runs = logviz.build_runs(cfg)
    by = {r["run_id"]: r for r in runs}
    assert [r["run_id"] for r in runs] == ["r4", "r3", "r2", "r1"]  # newest first
    r1 = by["r1"]
    assert r1["status"] == "failed" and r1["goal"] == "cheap earbuds"
    assert r1["retries"] == 1 and r1["failed_steps"] == 1
    s1, s2 = r1["steps"]
    assert s1["retried"] and [a["tier"] for a in s1["attempts"]] == ["cache", "local"]
    assert not s2["ok"] and s2["shot"] == "r1_fail_1.jpg" and s2["url"] == "https://x.test/s"
    assert [a["note"] for a in s2["attempts"]] == ["2 elements match", "not JSON"]
    assert r1["local_tokens"] == 320
    assert by["r2"]["status"] == "ok" and not by["r2"]["retries"]
    assert by["r3"]["status"] == "ok" and by["r3"]["goal"] == "manual step: click apply"
    assert by["r4"]["status"] == "needs_human" and by["r4"]["steps"][0]["reason"] == "bot check"
    assert abs(r1["expires_at"] - (r1["end"] + 24 * 3600)) < 1
    s = logviz.summary(runs)
    assert s["by_status"] == {"needs_human": 1, "ok": 2, "failed": 1}, s
    assert s["steps_failed"] == 2 and s["steps_retried"] == 1
    assert ("click sort by price", 1) in s["top_failures"]


def test_shot_path_safe():
    cfg = _cfg()
    os.makedirs(logviz.shots_dir(cfg))
    open(os.path.join(logviz.shots_dir(cfg), "ok_1.jpg"), "wb").write(b"x")
    assert logviz.shot_path(cfg, "ok_1.jpg")
    for bad in ("../steps.jsonl", "..%2Fx.jpg", "a/b.jpg", "x.png", "", None):
        assert logviz.shot_path(cfg, bad) is None, bad


def test_server():
    from http.server import ThreadingHTTPServer

    cfg = _cfg()
    StepLog(cfg.log_path, run_id="z").write("run", ok=True, goal="g")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = logviz.make_handler(cfg, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert "Browser Engine Logs" in urllib.request.urlopen(base + "/").read().decode()
        data = json.loads(urllib.request.urlopen(base + "/api/logs").read())
        assert data["runs"][0]["goal"] == "g"
        try:
            urllib.request.urlopen(base + "/shots/..%2Fsteps.jsonl")
            raise AssertionError("served a non-shot file")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        req = urllib.request.Request(base + "/api/logs", headers={"Host": "evil.test"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("wrong Host accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
