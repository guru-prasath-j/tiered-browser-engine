"""Run: PLAYWRIGHT_BROWSERS_PATH=... python -m pytest tests -q   (or python tests/test_engine.py)

The live test uses a fake 'local model' that picks elements by keyword, so no Ollama or
API key is needed. It checks the key promise: run 1 learns, run 2 is all cache hits.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.cache import ActionCache, norm_intent  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.engine import Engine, StepLog  # noqa: E402
from browser_engine.llm import Usage, parse_json  # noqa: E402
from browser_engine.observer import page_key  # noqa: E402
from browser_engine.report import summarize  # noqa: E402

PAGE = """<!doctype html><html><body>
<h1>Jobs</h1>
<input id="q" placeholder="Search jobs" aria-label="Search jobs">
<button id="go" onclick="document.getElementById('res').innerHTML =
  '<a href=\\'#job-48213\\'>Flutter developer at Acme</a>'">Search</button>
<select name="city"><option>Chennai</option><option>Bengaluru</option></select>
<label><input type="checkbox" name="remote"> Remote only</label>
<div id="res"></div>
</body></html>"""


def test_page_key():
    assert page_key("https://x.com/jobs/48213?a=1") == ("x.com", "/jobs/:id")
    assert page_key("https://X.com/u/3f2a9c1b-aaaa-bbbb-cccc-1234567890ab/edit")[1] == "/u/:id/edit"
    assert page_key("https://x.com/")[1] == "/"


def test_norm_and_parse():
    assert norm_intent("  Click   the Search button. ") == "click the search button"
    assert parse_json('```json\n{"action":"click","ref":"e1"}\n```')["ref"] == "e1"
    assert parse_json('Sure! {"action":"done"} hope that helps')["action"] == "done"


def test_cache_heals():
    with tempfile.TemporaryDirectory() as d:
        c = ActionCache(os.path.join(d, "c.sqlite"))
        c.put("x.com", "/", "click search", {"action": "click", "selectors": ["#go"]})
        assert c.get("x.com", "/other", "click search")[1]["selectors"] == ["#go"]  # domain fallback
        c.fail("x.com", "/", "click search")
        assert c.get("x.com", "/", "click search") is not None
        c.fail("x.com", "/", "click search")
        assert c.get("x.com", "/", "click search") is None  # dropped after 2 fails


class FakeLocal:
    """Stands in for Ollama: picks the element whose name matches intent keywords."""

    calls = 0

    def decide(self, intent, value, obs, feedback=None):
        FakeLocal.calls += 1
        i = intent.lower()

        def find(pred):
            return next((e.ref for e in obs.elements if pred(e)), None)

        if "search box" in i:
            d = {"action": "type", "ref": find(lambda e: e.role == "textbox")}
        elif "search button" in i:
            d = {"action": "click", "ref": find(lambda e: e.name == "Search")}
        elif "city" in i:
            d = {"action": "select", "ref": find(lambda e: e.role == "combobox")}
        elif "remote" in i:
            d = {"action": "check", "ref": find(lambda e: e.role == "checkbox")}
        elif "first job" in i:
            d = {"action": "click", "ref": find(lambda e: e.role == "link")}
        else:
            d = {"action": "fail", "reason": "unknown"}
        return json.dumps(d), Usage(len(obs.text) // 4, 30)


PLAN = [
    {"intent": "type into the search box", "value": "flutter"},
    {"intent": "click the search button", "value": None},
    {"intent": "select the city", "value": "Bengaluru"},
    {"intent": "check remote only", "value": None},
    {"intent": "click the first job result", "value": None},
]


def test_live_run_learns_then_replays():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d:
        html = os.path.join(d, "jobs.html")
        with open(html, "w") as fh:
            fh.write(PAGE)
        url = "file://" + html
        cfg = Config()
        cfg.data_dir, cfg.settle_ms = d, 100
        cache = ActionCache(cfg.cache_path)

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for run in (1, 2):
                page = browser.new_page()
                log = StepLog(cfg.log_path, run_id=f"run{run}")
                FakeLocal.calls = 0
                eng = Engine(page, cfg, cache, log, local=FakeLocal(), claude=None)
                summary = eng.run("search flutter jobs", start_url=url, plan=PLAN)
                assert summary["ok"]
                assert page.url.endswith("#job-48213")
                assert page.locator("select").input_value() == "Bengaluru"
                if run == 1:
                    assert summary["tiers"] == {"local": 5} and FakeLocal.calls == 5
                else:
                    assert summary["tiers"] == {"cache": 5} and FakeLocal.calls == 0
                page.close()
            browser.close()

        s1, s2 = summarize(cfg.log_path, run_id="run1"), summarize(cfg.log_path, run_id="run2")
        assert s1["claude_tokens"] == 0 and s1["local_tokens_free"] > 0
        assert s2["local_tokens_free"] == 0 and s2["by_tier"] == {"cache": 5}
        print("\nrun1:", s1["by_tier"], "local tokens:", s1["local_tokens_free"])
        print("run2:", s2["by_tier"], "local tokens:", s2["local_tokens_free"])
        print("cached:", [r[2] for r in cache.list()])


class FailingLocal:
    def decide(self, *a, **k):
        return '{"action": "fail", "reason": "not sure"}', Usage(100, 10)


class FakeClaude:
    """Escalation target; also proves the cache heals a broken selector."""

    def decide_text(self, intent, value, obs, feedback=None):
        return FakeLocal().decide(intent, value, obs, feedback)

    def decide_vision(self, *a, **k):
        raise AssertionError("vision should not be needed")


def test_escalation_and_budget_accounting():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d:
        html = os.path.join(d, "jobs.html")
        with open(html, "w") as fh:
            fh.write(PAGE)
        cfg = Config()
        cfg.data_dir, cfg.settle_ms = d, 100
        cache = ActionCache(cfg.cache_path)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=FailingLocal(), claude=FakeClaude())
            s = eng.run("x", start_url="file://" + html, plan=PLAN[:2])
            assert s["tiers"] == {"claude_text": 2} and s["claude_tokens"] > 0
            # Break the cached selectors: rename the button. Cache should miss, re-learn, heal.
            page.goto("file://" + html)
            page.evaluate("document.getElementById('go').id = 'renamed'; "
                          "document.getElementById('renamed').textContent = 'Find jobs'")
            eng2 = Engine(page, cfg, cache, StepLog(cfg.log_path), local=FakeLocalFind(), claude=None)
            eng2.do_step("click the search button")
            assert eng2.tiers_used == {"local": 1}
            browser.close()


class FakeLocalFind(FakeLocal):
    def decide(self, intent, value, obs, feedback=None):
        ref = next(e.ref for e in obs.elements if e.name == "Find jobs")
        return json.dumps({"action": "click", "ref": ref}), Usage(50, 10)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
