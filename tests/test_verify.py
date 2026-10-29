"""Verifier fixes found by the live benchmark (bench/): false failures and false passes."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.cache import ActionCache  # noqa: E402
from browser_engine.config import Config  # noqa: E402
from browser_engine.engine import Engine, StepLog  # noqa: E402
from browser_engine.executor import url_has, wrong_target  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.observer import Element  # noqa: E402


def els(*names):
    return [Element(ref=f"e{i}", role="link", name=n, tag="a") for i, n in enumerate(names)]


def test_url_check_ignores_case_and_separators():
    # books.toscrape: the model guessed 'Travel', the URL is .../travel_2/
    assert url_has("https://books.toscrape.com/catalogue/category/books/travel_2/index.html", "Travel")
    assert url_has("https://x.io/search?q=flutter+developer", "flutter developer")
    assert not url_has("https://x.io/jobs", "careers")


def test_wrong_target_catches_a_wrong_link():
    # Hacker News: "click the new link" landed on a story's site link
    e = els("Hacker News", "new", "past", "novaagentos.ai")
    assert wrong_target("click the new link in the top bar", e[3], e) == (e[3], e[1])
    assert wrong_target("click the new link in the top bar", e[1], e) is None


def test_wrong_target_leaves_generic_and_matching_picks_alone():
    e = els("Next →", "Senior Flutter Engineer", "Travel", "Mystery")
    assert wrong_target("click the Next page link", e[0], e) is None          # label matches
    assert wrong_target("click the first job result", e[1], e) is None        # nothing is named
    assert wrong_target("click the Travel category in the sidebar", e[2], e) is None
    assert wrong_target("click the Travel category in the sidebar", e[3], e) == (e[3], e[2])


PAGE = """<!doctype html><html><body>
<a href="#home">Hacker News</a> <a href="#new">new</a> <a href="#past">past</a>
<p>1. Some story <a href="#from-site">example.com</a></p>
<input aria-label="Search" placeholder="Search">
</body></html>"""


class WrongThenRight:
    """Picks the site link first (what qwen2.5:7b did live), then the right one after feedback."""
    calls = []

    def decide(self, intent, value, obs, feedback=None):
        WrongThenRight.calls.append(feedback)
        by = {e.name: e.ref for e in obs.elements}
        if intent.startswith("type"):
            # also guesses a URL change that typing can never cause
            return json.dumps({"action": "type", "ref": by["Search"], "expect": {"url_contains": "Search"}}), Usage(40, 10)
        ref = by["example.com"] if feedback is None else by["new"]
        return json.dumps({"action": "click", "ref": ref}), Usage(40, 10)


def test_engine_retries_a_wrong_click_and_caches_the_right_one():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d:
        html = os.path.join(d, "hn.html")
        with open(html, "w") as fh:
            fh.write(PAGE)
        cfg = Config()
        cfg.data_dir, cfg.settle_ms = d, 50
        cache = ActionCache(cfg.cache_path)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=WrongThenRight(), claude=None)
            s = eng.run("hn new", start_url="file://" + html,
                        plan=[{"intent": "type into the search box", "value": "flutter"},
                              {"intent": "click the new link in the top bar", "value": None}])
            assert s["ok"] and page.url.endswith("#new")
            assert WrongThenRight.calls[1] is None and "matches the intent" in WrongThenRight.calls[2]
            # replay from cache goes straight to the right link
            page.goto("file://" + html)
            eng2 = Engine(page, cfg, cache, StepLog(cfg.log_path), local=None, claude=None)
            eng2.do_step("click the new link in the top bar")
            assert eng2.tiers_used == {"cache": 1} and page.url.endswith("#new")
            browser.close()
