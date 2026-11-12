"""Home agent: settings file, model choice, local chat page safety. No browser or LLM needed.

    python tests/test_home.py      (needs .[home])
"""
import asyncio
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from browser_engine import home_agent as h  # noqa: E402


def test_env_roundtrip():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "home.env")
    h.save_env({"GOOGLE_API_KEY": "abc123"}, p)
    h.save_env({"BE_HOME_MODEL": "gemini-2.5-flash"}, p)  # keeps the earlier key
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    for k in ("GOOGLE_API_KEY", "BE_HOME_MODEL"):
        os.environ.pop(k, None)
    got = h.load_env(p)
    assert got == {"GOOGLE_API_KEY": "abc123", "BE_HOME_MODEL": "gemini-2.5-flash"}, got
    os.environ["BE_HOME_MODEL"] = "keep-me"
    os.environ.pop("GOOGLE_API_KEY")
    h.load_env(p)
    assert os.environ["BE_HOME_MODEL"] == "keep-me"  # real env vars win
    for k in ("GOOGLE_API_KEY", "BE_HOME_MODEL"):
        os.environ.pop(k, None)


def test_model_choice():
    for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "BE_HOME_MODEL"):
        os.environ.pop(k, None)
    assert h.model_name().startswith("ollama_chat/")
    os.environ["GEMINI_API_KEY"] = "x"
    assert h.model_name() == h.DEFAULT_GEMINI
    assert os.environ["GOOGLE_API_KEY"] == "x"
    os.environ["BE_HOME_MODEL"] = "groq/llama-3.3-70b-versatile"
    assert h.model_name() == "groq/llama-3.3-70b-versatile"
    for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "BE_HOME_MODEL"):
        os.environ.pop(k, None)


def test_steps_parse():
    assert h._parse_steps(["type into search :: shoes", "press Enter", " "]) == [
        {"intent": "type into search", "value": "shoes"}, {"intent": "press Enter", "value": None}]


def test_chat_page():
    from starlette.testclient import TestClient

    seen = []

    async def ask(sid, text):
        seen.append((sid, text))
        return sid or "s1", "reply to " + text

    port = 8770
    c = TestClient(h.build_app(ask, port, {"model": "m"}), base_url=f"http://127.0.0.1:{port}")
    assert "Browser Helper" in c.get("/").text
    assert c.get("/api/status").json() == {"model": "m"}
    hdr = {"X-BE-Chat": "1"}
    r = c.post("/api/chat", json={"message": "hi"}, headers=hdr)
    assert r.json() == {"session_id": "s1", "reply": "reply to hi"}, r.text
    # no custom header (plain cross-site form post) -> refused
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 403
    # another website's origin -> refused
    bad = dict(hdr, Origin="https://evil.example")
    assert c.post("/api/chat", json={"message": "hi"}, headers=bad).status_code == 403
    # DNS rebinding: wrong Host -> refused
    reb = TestClient(h.build_app(ask, port), base_url=f"http://evil.example:{port}")
    assert reb.get("/").status_code == 403
    assert c.post("/api/chat", json={"message": "  "}, headers=hdr).status_code == 400
    assert len(seen) == 1

    async def boom(sid, text):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    c2 = TestClient(h.build_app(boom, port), base_url=f"http://127.0.0.1:{port}")
    r = c2.post("/api/chat", json={"message": "hi"}, headers=hdr)
    assert r.status_code == 500 and "free Gemini limit" in r.json()["error"]


def test_agent_builds():
    agent = h.build_agent("gemini-2.5-flash")
    names = {getattr(t, "__name__", "") for t in agent.tools}
    assert {"run_browser_plan", "wait_for_human", "check_form", "extract_listings"} <= names
    assert "user_confirmed" in agent.instruction


def test_tool_errors_become_results():
    class Svc:
        def run_plan(self, *a, **kw):
            raise RuntimeError("Chrome went away")

    old = h._svc
    h._svc = Svc()
    try:
        out = asyncio.run(h.run_browser_plan("g", ["press Enter"]))
    finally:
        h._svc = old
    assert out["ok"] is False and "Chrome went away" in out["error"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
