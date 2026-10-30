"""Bot-check handoff tests. Run: python tests/test_human.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine import human  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.service import BrowserService  # noqa: E402

# A "press and hold" wall that the "user" clears after `ms` milliseconds.
WALL = """<!doctype html><html><head><title>Site</title></head><body>
<h1 id=h>Are you a person or a robot?</h1><p>Press & Hold to confirm</p>
<script>setTimeout(() => { document.body.innerHTML =
  '<h1>Results</h1><button id=b onclick="document.title=\\'clicked\\'">Show cheapest</button>'; }, MS);</script>
</body></html>"""


class ClickLocal:
    def decide(self, intent, value, obs, feedback=None):
        ref = next((e.ref for e in obs.elements if e.name == "Show cheapest"), None)
        if not ref:
            return json.dumps({"action": "fail", "reason": "CAPTCHA present"}), Usage(50, 5)
        return json.dumps({"action": "click", "ref": ref}), Usage(50, 5)


def _svc(pw, wait_s):
    browser = pw.chromium.launch()
    cfg = Config(data_dir=tempfile.mkdtemp())
    cfg.human_wait_s = wait_s
    return BrowserService(cfg, local=ClickLocal(), claude=None, page_factory=browser.new_page), browser


def test_detect():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(); p = b.new_page()
        p.set_content("<h1>Are you a person or a robot?</h1>")
        assert human.detect(p)
        p.set_content("<title>Just a moment...</title><p>x</p>")
        assert human.detect(p)
        p.set_content("<h1>Bengaluru to Dubai</h1><button>Search</button>")
        assert human.detect(p) is None
        b.close()


def test_resumes_after_user_solves():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        svc, b = _svc(pw, wait_s=15)
        page = svc._get_page()
        page.set_content(WALL.replace("MS", "3000"))
        out = svc.run_plan("cheapest", [{"intent": "click show cheapest"}])
        assert out["ok"], out
        assert out["human_checks_solved"] == 1
        assert page.title() == "clicked"
        b.close()


def test_hands_back_when_not_solved_in_time():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        svc, b = _svc(pw, wait_s=2)
        page = svc._get_page()
        page.set_content(WALL.replace("MS", "6000"))
        out = svc.run_plan("cheapest", [{"intent": "click show cheapest"}, {"intent": "read price"}])
        assert out["needs_human"] and len(out["remaining_steps"]) == 2, out
        w = svc.wait_human(timeout_s=10)
        assert w["resolved"], w
        out = svc.run_plan("cheapest", out["remaining_steps"][:1])
        assert out["ok"], out
        b.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
