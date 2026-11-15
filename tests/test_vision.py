"""Layer 2 (screenshots only): runs when the HTML is unreadable or layer 1 gives up.

Real Chromium pages, scripted vision model (so no API key or Ollama is needed).
    python tests/test_vision.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["BE_DATA_DIR"] = tempfile.mkdtemp()

from playwright.sync_api import sync_playwright  # noqa: E402

from browser_engine import vision  # noqa: E402
from browser_engine.cache import ActionCache  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.engine import Engine, StepFailed, StepLog  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.safety import Blocked, Guard  # noqa: E402

# A page with no readable elements: everything is drawn on a canvas.
CANVAS = """<html><body style="margin:0;background:#fff">
<canvas id="c" width="800" height="400"></canvas>
<script>
const c = document.getElementById('c'), x = c.getContext('2d');
function draw(msg){ x.fillStyle='#fff'; x.fillRect(0,0,800,400);
  x.fillStyle='#1f5f4a'; x.fillRect(300,160,200,60); x.fillStyle='#fff'; x.font='24px sans-serif';
  x.fillText('Search', 360, 198); if(msg){x.fillStyle='#000'; x.fillText(msg, 280, 300);} }
draw('');
c.addEventListener('click', e => { if (e.offsetX>300 && e.offsetX<500 && e.offsetY>160 && e.offsetY<220) draw('Results: 3 items'); });
document.addEventListener('keydown', e => { if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) { window.typed = (window.typed||'') + e.key; draw('typed: ' + window.typed); } });
</script></body></html>"""

# A normal page where layer 1 can't find the target (it's painted, not an element).
MIXED = """<html><body style="margin:0"><a href="#" style="position:absolute;left:10px;top:10px">Home</a>
<canvas id="c" width="600" height="300" style="position:absolute;left:0;top:60px"></canvas>
<script>const x=document.getElementById('c').getContext('2d');x.fillStyle='#c00';x.fillRect(100,100,160,50);
document.getElementById('c').onclick=e=>{x.fillStyle='#000';x.font='22px sans-serif';x.fillText('Applied filter',300,130);};</script>
</body></html>"""


class Scripted:
    """Stands in for a vision model: replies in order, records what it was shown."""

    def __init__(self, *replies, raise_error=None):
        self.replies = list(replies)
        self.raise_error = raise_error
        self.seen = []

    def decide(self, system, prompt, img_b64):
        self.seen.append((system, prompt, len(img_b64)))
        if self.raise_error:
            raise self.raise_error
        r = self.replies.pop(0)
        return (r if isinstance(r, str) else json.dumps(r)), Usage(900, 40)


def load(page, html):
    """Fresh window per page (set_content alone keeps the old window and its globals)."""
    import base64

    page.goto("data:text/html;base64," + base64.b64encode(html.encode()).decode())


def _engine(page, providers, confirm=False):
    cfg = Config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    return Engine(page, cfg, ActionCache(cfg.cache_path), StepLog(cfg.log_path), local=None, claude=None,
                  guard=Guard(confirm), vision=vision.VisionLayer(providers, tries=2))


def _log(eng):
    with open(eng.cfg.log_path) as fh:
        return [json.loads(line) for line in fh if json.loads(line)["run_id"] == eng.log.run_id]


def run(page):
    # 1. Blind page -> layer 2 straight away; click on the canvas works, verified by pixels.
    load(page, CANVAS)
    m = Scripted({"action": "click", "x": 400, "y": 190, "target": "Search", "reason": "button"})
    eng = _engine(page, [("vision_local", m)])
    assert eng.do_step("click the Search button") == "vision_local"
    system, prompt, size = m.seen[0]
    assert "800 x" in system or "x 600" in system or "pixels" in system and size > 1000
    assert "Intent: click the Search button" in prompt
    tiers = [r.get("tier") for r in _log(eng) if r["kind"] == "attempt"]
    assert tiers == ["vision_local"], tiers  # no cache / match / local tiers on a blind page
    print("ok blind page -> screenshot click")

    # 2. First model misses (nothing changes), second model fixes it with the feedback.
    load(page, CANVAS)
    bad = Scripted({"action": "click", "x": 50, "y": 50, "target": "Search"},
                   {"action": "fail", "reason": "can't find it"})
    good = Scripted({"action": "click", "point": [317, 500], "target": "Search"})  # Gemini [y, x] /1000
    eng = _engine(page, [("vision_local", bad), ("vision_gemini", good)])
    assert eng.do_step("click Search") == "vision_gemini"
    assert "screen did not change" in bad.seen[1][1]  # retry got the failure as feedback
    atts = [(r["tier"], r["ok"]) for r in _log(eng) if r["kind"] == "attempt"]
    assert atts == [("vision_local", False), ("vision_local", False), ("vision_gemini", True)], atts
    print("ok retry with feedback, then next vision model")

    # 3. Typing by coordinates, verified by pixels around the field.
    load(page, CANVAS)
    t = Scripted({"action": "type", "x": 400, "y": 300, "target": "search box", "field": "search"})
    eng = _engine(page, [("vision_local", t)])
    assert eng.do_step("type into the search box", "earbuds") == "vision_local"
    assert page.evaluate("window.typed") == "earbuds"
    print("ok type by coordinates")

    # 4. Reading data from the screenshot.
    load(page, CANVAS)
    r = Scripted({"action": "extract", "text": "Search", "reason": "button label"})
    eng = _engine(page, [("vision_local", r)])
    eng.intent = "read the button label"
    eng.do_step("read the button label")
    assert eng.extracted[-1]["text"] == "Search" and eng.extracted[-1]["source"] == "screenshot"
    print("ok read from screenshot")

    # 5. Safety: password fields and irreversible clicks are refused in layer 2 too.
    load(page, CANVAS)
    eng = _engine(page, [("vision_local", Scripted({"action": "type", "x": 400, "y": 190,
                                                   "target": "Password", "field": "password"}))])
    try:
        eng.do_step("type into the login field", "hunter2")
        raise AssertionError("typed into a password field")
    except Blocked:
        pass
    assert not page.evaluate("window.typed || ''")
    eng = _engine(page, [("vision_local", Scripted({"action": "click", "x": 400, "y": 190,
                                                   "target": "Pay now"}))])
    try:
        eng.do_step("click the button")
        raise AssertionError("clicked Pay without confirmation")
    except Blocked as e:
        assert "pay" in str(e).lower()
    eng = _engine(page, [("vision_local", Scripted({"action": "click", "x": 400, "y": 190,
                                                   "target": "Pay now"}))], confirm=True)
    assert eng.do_step("click the button") == "vision_local"  # allowed once the user said yes
    print("ok guard: password + irreversible")

    # 6. Normal page, layer 1 has no element for it -> falls through to layer 2.
    load(page, MIXED)
    f = Scripted({"action": "click", "x": 180, "y": 185, "target": "red filter"})
    eng = _engine(page, [("vision_local", f)])
    assert eng.do_step("click the red filter") == "vision_local"
    kinds = [r["kind"] for r in _log(eng)]
    assert "layer" in kinds, kinds
    print("ok layer 1 gives up -> layer 2")

    # 7. Model unreachable -> next one; all fail -> StepFailed (not a crash).
    load(page, CANVAS)
    down = Scripted(raise_error=ConnectionError("ollama not running"))
    nope = Scripted({"action": "fail", "reason": "no such thing"})
    eng = _engine(page, [("vision_local", down), ("vision_gemini", nope)])
    try:
        eng.do_step("click the Checkout button")
        raise AssertionError("should fail")
    except StepFailed as e:
        assert "no readable elements" in str(e)
    notes = [r.get("note", "") for r in _log(eng) if r["kind"] == "attempt"]
    assert "unavailable" in notes[0] and "gave up" in notes[1], notes
    print("ok unavailable model / all fail")

    # 8. A hover highlight alone is not success: the "before" shot is taken while hovering.
    load(page, """<html><body style="margin:0"><style>#d:hover{background:#c00}</style>
<canvas width="10" height="10"></canvas>
<div id="d" style="position:absolute;left:300px;top:150px;width:200px;height:80px;background:#08c"></div></body></html>""")
    h = Scripted({"action": "click", "x": 400, "y": 190, "target": "dead box"}, {"action": "fail", "reason": "-"})
    eng = _engine(page, [("vision_local", h)])
    try:
        eng.do_step("click the dead box")
        raise AssertionError("hover counted as a click effect")
    except StepFailed:
        pass
    print("ok hover-only change is not success")

    # 9. Layer 2 off -> blind page fails in layer 1 as before.
    load(page, CANVAS)
    cfg = Config()
    eng = Engine(page, cfg, ActionCache(cfg.cache_path), StepLog(cfg.log_path), guard=Guard(False))
    try:
        eng.do_step("click the Search button")
        raise AssertionError("should fail without layer 2")
    except StepFailed:
        pass
    print("ok no layer 2 configured")


def test_coords():
    assert vision.coords({"x": 10, "y": 20}, (800, 600)) == (10.0, 20.0)
    assert vision.coords({"x": 0.5, "y": 0.25}, (800, 600)) == (400.0, 150.0)
    assert vision.coords({"point": [500, 250]}, (800, 600)) == (200.0, 300.0)
    assert vision.coords({"x": 900, "y": 20}, (800, 600)) is None
    assert vision.coords({"x": "abc", "y": 2}, (800, 600)) is None
    print("ok coords")


def test_providers_env():
    for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "BE_ALLOW_CLAUDE_API"):
        os.environ.pop(k, None)
    os.environ["BE_NO_LOCAL"] = "1"
    assert vision.providers(Config()) == []
    os.environ["GEMINI_API_KEY"] = "k"
    assert [n for n, _ in vision.providers(Config())] == ["vision_gemini"]
    os.environ["BE_VISION"] = "0"
    assert vision.providers(Config()) == []
    for k in ("GEMINI_API_KEY", "BE_VISION", "BE_NO_LOCAL"):
        os.environ.pop(k, None)
    print("ok providers from env")


if __name__ == "__main__":
    test_coords()
    test_providers_env()
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 800, "height": 600})
        try:
            run(page)
        finally:
            b.close()
