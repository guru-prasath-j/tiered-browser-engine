"""Careful mode: look first (cached), close blockers safely, act through the HTML, check reads.

Real Chromium pages, scripted local vision model (no Ollama needed).
    python tests/test_careful.py
"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.sync_api import sync_playwright  # noqa: E402

from browser_engine import careful, vision  # noqa: E402
from browser_engine.cache import ActionCache  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.engine import Engine, StepLog  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.popups import PopupMemory  # noqa: E402
from browser_engine.safety import Guard  # noqa: E402

POPUP = """<html><body style="font-family:sans-serif;padding:30px"><h2>Software Engineer</h2>
<button onclick="this.textContent='Applied'">I'm interested</button>
<div id="ov" style="position:fixed;inset:0;background:rgba(0,0,0,.45)">
 <div style="background:#fff;width:320px;margin:140px auto;padding:20px">Get job alerts!
  <span onclick="document.getElementById('ov').remove()" style="cursor:pointer;color:#06c">Maybe later</span>
  <span onclick="window.accepted=1" style="cursor:pointer">Accept offer</span></div></div></body></html>"""

TWO_APPLY = """<html><body style="font-family:sans-serif;margin:0">
<div style="position:absolute;left:20px;top:20px"><button onclick="window.clicked='sponsored'">Apply</button> <small>Sponsored</small></div>
<div style="position:absolute;left:400px;top:300px"><h3>Flutter Developer</h3>
<button onclick="window.clicked='real';this.textContent='Applying'">Apply</button></div></body></html>"""

PRICES = """<html><body style="font-family:sans-serif;padding:30px">
<a href="#f1">IndiGo 6E-511 · 06:10 → 09:05 · ₹4,899</a></body></html>"""


class Eyes:
    """Scripted local vision model: separate reply queues for look / act / read-check prompts."""

    def __init__(self, looks=(), acts=(), checks=()):
        self.q = {"look": list(looks), "act": list(acts), "check": list(checks)}
        self.calls = {"look": 0, "act": 0, "check": 0}
        self.model = "scripted-vl"

    def decide(self, system, prompt, img_b64):
        kind = "look" if "eyes of a browser" in system else "check" if "check data" in system else "act"
        self.calls[kind] += 1
        if not self.q[kind]:
            raise AssertionError(f"unexpected {kind} call (should have come from the cache)")
        return json.dumps(self.q[kind].pop(0)), Usage(1200, 60)


def load(page, html):
    page.goto("data:text/html;base64," + base64.b64encode(html.encode()).decode())


def engine(page, eyes, cfg, mode="careful", confirm=False):
    cfg.mode = mode
    layer = vision.VisionLayer([("vision_local", eyes)], tries=1,
                               look_cache=careful.LookCache(cfg.cache_path))
    return Engine(page, cfg, ActionCache(cfg.cache_path), StepLog(cfg.log_path), guard=Guard(confirm),
                  popups=PopupMemory(cfg.popups_path), vision=layer)


def log(eng):
    with open(eng.cfg.log_path) as fh:
        return [json.loads(x) for x in fh if json.loads(x)["run_id"] == eng.log.run_id]


def fresh_cfg():
    os.environ["BE_DATA_DIR"] = tempfile.mkdtemp()
    cfg = Config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.popup_wait_ms = 0
    return cfg


def run(page):
    # 1. The stuck case: a popup covers the button. Eyes see it, close it with "Maybe later",
    #    look again, and the click goes to the real button (no DOM click through the popup).
    cfg = fresh_cfg()
    load(page, POPUP)
    eyes = Eyes(looks=[
        {"blocker": {"present": True, "kind": "newsletter popup", "close_label": "Maybe later", "x": 0, "y": 0},
         "target": {"found": False}, "state": "job alerts popup over a job page"},
        {"blocker": {"present": False}, "target": {"found": True, "label": "I'm interested", "role": "button",
                                                   "x": 90, "y": 95}, "state": "job page"}])
    eng = engine(page, eyes, cfg)
    tier = eng.do_step("click the I'm interested button")
    assert page.evaluate("document.querySelector('button').textContent") == "Applied"
    assert not page.evaluate("!!window.accepted")
    recs = log(eng)
    pop = [r for r in recs if r["kind"] == "popup"]
    assert pop and pop[0]["closed"] == ["Maybe later"] and pop[0]["via"].startswith("eyes"), pop
    notes = " ".join(r.get("note", "") for r in recs if r["kind"] == "attempt")
    assert "DOM click" not in notes, notes
    assert eyes.calls["look"] == 2 and tier in ("match", "eyes"), (eyes.calls, tier)
    print("ok popup closed by the eyes, then the real click")

    # 2. Same task again: every look comes from the cache (no vision calls at all).
    load(page, POPUP)
    eyes2 = Eyes()  # no replies: any call fails the test
    eng = engine(page, eyes2, cfg)
    eng.do_step("click the I'm interested button")
    assert page.evaluate("document.querySelector('button').textContent") == "Applied"
    assert eyes2.calls == {"look": 0, "act": 0, "check": 0}, eyes2.calls
    looks = [r for r in log(eng) if r.get("tier") == "look"]
    assert looks and all(r.get("cached") for r in looks), looks
    print("ok repeat run: looks from cache, zero vision calls")

    # 3. Two look-alike "Apply" buttons: label match can't choose; the eyes say which one.
    cfg = fresh_cfg()
    load(page, TWO_APPLY)
    eyes = Eyes(looks=[{"blocker": {"present": False},
                        "target": {"found": True, "label": "Apply", "role": "button", "x": 425, "y": 360},
                        "state": "job page with a sponsored apply at the top"}])
    eng = engine(page, eyes, cfg)
    assert eng.do_step("click the apply button") == "eyes"
    assert page.evaluate("window.clicked") == "real"
    print("ok eyes pick the right one of two look-alike buttons")

    # 4. A popup whose only way out is "Accept offer": never clicked.
    cfg = fresh_cfg()
    load(page, POPUP.replace(">Maybe later<", "><"))
    eyes = Eyes(looks=[{"blocker": {"present": True, "kind": "promo", "close_label": "Accept offer", "x": 0, "y": 0},
                        "target": {"found": False}, "state": "promo"}])
    eng = engine(page, eyes, cfg)
    try:
        eng.do_step("click the I'm interested button")
    except Exception:
        pass
    assert not page.evaluate("!!window.accepted")
    assert any("not a safe one" in r.get("note", "") for r in log(eng))
    print("ok unsafe close label is never clicked")

    # 5. Reading: data comes from the HTML, then is checked against the screenshot.
    cfg = fresh_cfg()
    load(page, PRICES)
    eyes = Eyes(looks=[{"blocker": {"present": False},
                        "target": {"found": True, "label": "IndiGo 6E-511 · 06:10 → 09:05 · ₹4,899", "role": "link",
                                   "x": 150, "y": 40}, "state": "one flight"}],
                checks=[{"match": False, "mismatches": ["screenshot shows ₹4,399, text says ₹4,899"],
                         "visible_values": ["₹4,399"]}])
    eng = engine(page, eyes, cfg)
    eng.do_step("read the cheapest flight")
    item = eng.extracted[-1]
    assert "4,899" in item["text"] and item.get("source") != "screenshot"
    assert item["screenshot_check"]["match"] is False and "4,399" in item["screenshot_check"]["mismatches"][0]
    rc = [r for r in log(eng) if r.get("tier") == "read_check"]
    assert rc and rc[0]["ok"] is False
    print("ok read from HTML, flagged by the screenshot check")

    # 6. Fast mode: no looking first.
    cfg = fresh_cfg()
    load(page, TWO_APPLY.replace(">Apply</button> <small>", ">Sponsored apply</button> <small>"))
    eyes = Eyes()
    eng = engine(page, eyes, cfg, mode="fast")
    eng.do_step("click the apply button")
    assert eyes.calls["look"] == 0
    print("ok fast mode does not look first")


def test_dhash():
    from PIL import Image, ImageDraw

    a = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(a).rectangle([100, 100, 300, 160], fill="navy")
    b = a.copy()
    ImageDraw.Draw(b).text((110, 120), "4899", fill="white")  # a small change: same screen
    c = a.copy()
    ImageDraw.Draw(c).rectangle([200, 150, 600, 450], fill="gray")  # a popup: different screen
    fa, fb, fc = careful.fingerprint(a), careful.fingerprint(b), careful.fingerprint(c)
    assert careful.same_screen(fa, fb)
    assert not careful.same_screen(fa, fc)
    # a semi-transparent dark overlay keeps the pattern but not the brightness
    d = Image.blend(a, Image.new("RGB", a.size, "black"), 0.45)
    assert careful.hamming(careful.dhash(a), careful.dhash(d)) <= careful.DHASH_DIST  # pattern alone is fooled
    assert not careful.same_screen(fa, careful.fingerprint(d))
    print("ok screen fingerprint: small change same, popup / dark overlay different")


def test_safe_close():
    for ok in ("Maybe later", "×", "No thanks", "Reject all", "Close", "Not now", "Continue as guest"):
        assert careful.SAFE_CLOSE.match(ok), ok
    for bad in ("Accept offer", "Accept all", "Sign up", "Continue", "Subscribe", "Yes", "Allow"):
        assert not careful.SAFE_CLOSE.match(bad), bad
    print("ok safe close labels")


if __name__ == "__main__":
    test_dhash()
    test_safe_close()
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 800, "height": 600})
        try:
            run(page)
        finally:
            b.close()
