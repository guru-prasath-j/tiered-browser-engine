"""Popup dismissal, guest mode and A2A server tests. Run: python tests/test_guest.py"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.config import Config  # noqa: E402
from browser_engine.guest import Busy, GuestBrowserService, UrlPolicy  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.popups import PopupMemory  # noqa: E402
from browser_engine.service import BrowserService  # noqa: E402

# A shop page whose Search button is covered by a cookie banner and a Google One Tap box.
SHOP = """<!doctype html><html><head><title>Shop</title></head><body>
<h1>Shop</h1><input aria-label="Search products" id=q>
<button id=s onclick="document.title='searched:'+document.getElementById('q').value">Search</button>
<div id="credential_picker_container" style="position:fixed;top:0;right:0;width:400px;height:300px;background:#fff;z-index:9">
  <iframe src="about:blank" style="width:100%;height:100%"></iframe></div>
<div class="cookie-banner" style="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10">
  <p>We use cookies</p><button onclick="this.parentElement.remove()">Reject all</button>
  <button onclick="this.parentElement.remove()">Accept all</button></div>
</body></html>"""

# A "sign in to save" popup that guests may skip, on a page that otherwise works without login.
SOFT_LOGIN = """<!doctype html><html><head><title>Flights</title></head><body>
<h1>Flights</h1><button onclick="document.title='found'">Find flights</button>
<div role="dialog" aria-modal="true" style="position:fixed;inset:0;background:#fff;z-index:10">
  <p>Sign in to save your searches</p><input type=password aria-label="Password">
  <button onclick="document.title='SIGNED IN'">Sign in</button>
  <button onclick="this.parentElement.remove()">Continue as guest</button></div>
</body></html>"""

LOGIN = """<!doctype html><html><head><title>Sign in</title></head><body>
<h1>Sign in</h1><input type=email aria-label="Email"><input type=password aria-label="Password">
<button>Next</button></body></html>"""

PAGES = {"https://shop.example/": SHOP, "https://flights.example/": SOFT_LOGIN,
         "https://accounts.google.com/signin": LOGIN, "https://news.example/login": LOGIN}


class ClickLocal:
    """Types into search boxes and clicks the button named in the intent, if it's actually clickable."""

    def decide(self, intent, value, obs, feedback=None):
        i = intent.lower()
        for e in sorted(obs.elements, key=lambda e: e.role not in ("button", "link")):
            n = e.name.lower()
            if value is not None and "search" in n and e.role in ("textbox", "searchbox", "input"):
                return json.dumps({"action": "type", "ref": e.ref, "text": value}), Usage(50, 5)
            if value is None and n and n in i:
                return json.dumps({"action": "click", "ref": e.ref}), Usage(50, 5)
        return json.dumps({"action": "fail", "reason": "not found"}), Usage(50, 5)


def _context(browser):
    ctx = browser.new_context()

    def serve(route):
        url = route.request.url
        html = PAGES.get(url) or PAGES.get(url.split("?")[0])
        if html:
            return route.fulfill(status=200, content_type="text/html", body=html)
        return route.abort()

    ctx.route("**/*", serve)
    return ctx


def _cfg(d):
    cfg = Config()
    cfg.data_dir, cfg.settle_ms, cfg.popup_wait_ms = d, 100, 100
    return cfg


def test_url_policy():
    p = UrlPolicy(resolve=False)
    for bad in ["http://localhost:3000", "http://127.0.0.1/", "http://192.168.1.5/router",
                "http://10.0.0.2", "http://100.101.102.103:3000", "http://[::1]/", "http://nas.local",
                "https://mac.tail1234.ts.net", "file:///etc/passwd", "chrome://settings", "http://intranet"]:
        assert p.why_blocked(bad), bad
    for ok in ["https://www.skyscanner.co.in/", "https://example.com/a?b=1", "data:text/html,hi"]:
        assert p.why_blocked(ok) is None, ok
    os.environ["BE_GUEST_ALLOW_DOMAINS"] = "skyscanner.co.in"
    try:
        p = UrlPolicy(resolve=False)
        assert p.why_blocked("https://www.skyscanner.co.in/") is None
        assert p.why_blocked("https://example.com/")
    finally:
        del os.environ["BE_GUEST_ALLOW_DOMAINS"]


def test_owner_mode_closes_popups_and_remembers():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d, sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = _context(browser)
        svc = BrowserService(_cfg(d), local=ClickLocal(), claude=None, page_factory=ctx.new_page)
        r = svc.run_plan("search", [{"intent": "type into search products", "value": "earbuds"},
                                    {"intent": "click search"}], start_url="https://shop.example/")
        assert r["ok"], r
        assert svc.page.title() == "searched:earbuds"
        assert "reject all" in r["popups_closed"] and "google one tap" in r["popups_closed"], r
        mem = PopupMemory(os.path.join(d, "popups.json"))
        assert mem.preferred("shop.example")[0] == "reject all"

        # Owner mode never clicks "continue as guest" (the owner may want to sign in).
        r = svc.run_plan("find", [{"intent": "click find flights"}], start_url="https://flights.example/")
        assert "continue as guest" not in r.get("popups_closed", []), r
        assert svc.page.title() != "SIGNED IN"
        browser.close()


def test_guest_mode_rules():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d, sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = _context(browser)
        g = GuestBrowserService(_cfg(d), local=ClickLocal(), page_factory=ctx.new_page)

        # Optional "sign in to save" popup: skipped as guest, task still works.
        r = g.run_plan("find", [{"intent": "click find flights"}], start_url="https://flights.example/")
        assert r["ok"], r
        assert "continue as guest" in r["popups_closed"] and g.page.title() == "found", r

        # Real login walls stop the task with login_required.
        for url in ["https://accounts.google.com/signin", "https://news.example/login"]:
            r = g.run_plan("read", [{"intent": "read the page"}], start_url=url)
            assert not r["ok"] and r.get("login_required"), r
            assert "page" not in r

        # Private addresses are refused before anything loads.
        r = g.run_plan("x", [{"intent": "read the page"}], start_url="http://192.168.1.1/")
        assert not r["ok"] and r["blocked"], r
        r = g.do_step("open router", "navigate", value="http://localhost:8080")
        assert not r["ok"] and r["blocked"], r

        # Irreversible actions stay blocked even if the caller asks to confirm.
        r = g.run_plan("s", [{"intent": "click search"}], start_url="https://shop.example/",
                       confirm_irreversible=True)
        assert r["ok"], r  # "search" is not irreversible
        assert g.cache_op() == {"error": "not available to remote users"}
        assert g.wait_human()["resolved"] is False
        browser.close()


def test_guest_browser_isolated_and_network_blocked():
    """The real guest browser: its own Chromium, empty cookies per task, LAN requests aborted."""
    import http.server
    import threading

    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret LAN page")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lan = f"http://127.0.0.1:{srv.server_port}/"
    with tempfile.TemporaryDirectory() as d:
        g = GuestBrowserService(_cfg(d), local=None, headless=True)
        page = g._get_page()
        page.set_content(f'<img src="{lan}pixel"><a href="{lan}">x</a>')
        page.wait_for_timeout(300)
        try:
            page.goto(lan)
            raise AssertionError("LAN navigation was not blocked")
        except Exception as e:
            assert "ERR_BLOCKED_BY_CLIENT" in str(e) or "blocked" in str(e).lower(), e
        assert hits == [], hits  # nothing reached the LAN server, not even the <img>

        page.context.add_cookies([{"name": "sid", "value": "1", "url": "https://shop.example/"}])
        g.reset()
        page2 = g._get_page()
        assert page2.context.cookies() == []  # next task starts clean
        g.reset()
        g._browser.close()
        g._pw.stop()
    srv.shutdown()


def test_busy_one_task_at_a_time():
    b = Busy(stale_s=60)
    assert b.acquire("a") and not b.acquire("b") and b.acquire("a")
    b.release("b")
    assert not b.acquire("b")
    b.release("a")
    assert b.acquire("b")
    b2 = Busy(stale_s=0)
    assert b2.acquire("a") and b2.acquire("b")  # a stuck task doesn't block forever


def test_a2a_server_auth_and_roundtrip():
    """Boot the real A2A app with a scripted model: card is public, everything else needs the token."""
    from typing import AsyncGenerator

    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from starlette.testclient import TestClient

    from browser_engine import a2a_agent

    class Scripted(BaseLlm):
        model: str = "scripted"

        async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator[LlmResponse, None]:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Cheapest: ₹4,850")]))

    os.environ["BE_DATA_DIR"] = tempfile.mkdtemp()
    agent = LlmAgent(name="browser_engine", model=Scripted(), instruction="x",
                     tools=[a2a_agent.run_browser_plan],
                     before_agent_callback=a2a_agent._before, after_agent_callback=a2a_agent._after)

    class FakeSvc:
        def reset(self):
            pass

    a2a_agent._svc = FakeSvc()
    app = a2a_agent.build_app("127.0.0.1", 8765, "s3cret", agent=agent)
    with TestClient(app) as c:
        card = c.get("/.well-known/agent-card.json")
        assert card.status_code == 200, card.text
        assert card.json()["name"] == "browser_engine"
        assert c.post("/", json={}).status_code == 401
        assert c.post("/", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401

        msg = {"role": "user", "parts": [{"kind": "text", "text": "cheapest flight"}],
               "messageId": "m1", "kind": "message"}
        hdr = {"Authorization": "Bearer s3cret"}
        r = c.post("/", headers=hdr, json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                          "params": {"message": msg}})
        if "error" in r.json():  # A2A 1.0 method names
            msg1 = {"role": "ROLE_USER", "parts": [{"text": "cheapest flight"}], "messageId": "m1"}
            r = c.post("/", headers={**hdr, "A2A-Version": "1.0"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg1}})
        body = r.json()
        assert r.status_code == 200 and "error" not in body, body
        assert "4,850" in json.dumps(body, ensure_ascii=False), body
        print("\nA2A reply:", json.dumps(body, ensure_ascii=False)[:300])
    a2a_agent._svc = None


def test_steps_parsing():
    from browser_engine.a2a_agent import _parse_steps

    assert _parse_steps(["type into the search box :: flights to Delhi", "press Enter", " "]) == [
        {"intent": "type into the search box", "value": "flights to Delhi"},
        {"intent": "press Enter", "value": None}]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
