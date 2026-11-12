"""Regression tests built from the real failures in ~/.browser_engine/steps.jsonl (Oct-Nov 2026)
and for the zero-token label-match tier."""
import os
import tempfile

import pytest

from browser_engine.cache import ActionCache
from browser_engine.config import Config
from browser_engine.engine import Engine, StepLog
from browser_engine.executor import locate_cached
from browser_engine import matcher
from browser_engine.observer import observe


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


def _setup(browser, html, d):
    path = os.path.join(d, "p.html")
    with open(path, "w") as fh:
        fh.write(html)
    cfg = Config()
    cfg.data_dir, cfg.settle_ms, cfg.popup_wait_ms = d, 50, 0
    page = browser.new_page()
    page.goto("file://" + path)
    return cfg, page, ActionCache(cfg.cache_path)


class NoModel:
    """Fails the test if any model is asked."""
    def decide(self, *a, **k):
        raise AssertionError("a model was called for a step the label matcher should solve")


HN = """<a href="#home">Hacker News</a> <a href="#new">new</a> <a href="#past">past</a>
<p>1. New research on things <a href="#from-site">example.com</a></p>
<p>2. Another story <a href="#s2">example.org</a></p>"""

FORM = """<form><p><label>Customer name: <input name="custname"></label></p>
<p><label>Telephone: <input type="tel" name="custtel"></label></p>
<fieldset><legend>Pizza Size</legend>
<label><input type="radio" name="size" value="small"> Small</label>
<label><input type="radio" name="size" value="medium"> Medium</label>
<label><input type="radio" name="size" value="large"> Large</label></fieldset></form>"""


def test_label_match_solves_named_steps_with_zero_model_calls(browser):
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, FORM, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=NoModel(), claude=None)
        for intent, value in [("type into the customer name field", "Guru Test"),
                              ("type into the telephone field", "5550100"),
                              ("click the Medium pizza size option", None)]:
            eng.do_step(intent, value)
        assert eng.tiers_used == {"match": 3}
        assert page.locator("input[name=custname]").input_value() == "Guru Test"
        assert page.locator("input[value=medium]").is_checked()
        # learned: a second run is a pure cache replay
        page.reload()
        eng2 = Engine(page, cfg, cache, StepLog(cfg.log_path), local=NoModel(), claude=None)
        eng2.do_step("type into the telephone field", "999888")
        assert eng2.tiers_used == {"cache": 1}


def test_label_match_picks_exact_label_over_story_titles(browser):
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, HN, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=NoModel(), claude=None)
        eng.do_step("click the new link in the top bar")
        assert page.url.endswith("#new") and eng.tiers_used == {"match": 1}


def test_label_match_stays_out_of_positional_and_ambiguous_steps(browser):
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, HN, d)
        obs = observe(page)
        assert matcher.match("click the first story", None, obs) is None
        assert matcher.match("click the example link", None, obs) is None  # .com vs .org: ambiguous
        assert matcher.match("read the first story title", None, obs) is None
        assert matcher.match("press enter to search", None, obs)["text"] == "Enter"
        assert matcher.match("go back", None, obs)["action"] == "back"


def test_wrong_url_guess_no_longer_fails_a_working_click_and_is_not_cached(browser):
    # Logged: "expected URL to contain '261029', got https://www.skyscanner..." although the click worked.
    html = '<a href="#results?date=2026-10-29">Search flights</a>'
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, html, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path))
        eng.intent = "click search flights"
        obs = observe(page)
        ref = obs.elements[0].ref
        ok, why, entry = eng.execute_decision(
            {"action": "click", "ref": ref, "expect": {"url_contains": "261029"}}, "click", None, obs)
        assert ok, why
        assert entry["expect"] == {}  # the wrong guess is not cached, so replays can't fail on it


def test_formatted_phone_field_counts_as_typed(browser):
    # Logged: "field holds '+91 80567 08293', not the value"
    html = """<label>Phone <input id="ph"></label><script>
    ph.addEventListener('input', e => { const d = e.target.value.replace(/\\D/g, '').slice(-10);
      e.target.value = '+91 ' + d.slice(0, 5) + ' ' + d.slice(5); });</script>"""
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, html, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=NoModel())
        eng.do_step("type into the phone field", "8056708293")
        assert page.locator("#ph").input_value() == "+91 80567 08293"


def test_several_refs_from_a_model_use_the_first_valid_one(browser):
    # Logged: "ref 'e20,e21,e22,e23,e24' is not in the element list"
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, HN, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path))
        eng.intent = "click new"
        obs = observe(page)
        new_ref = next(e.ref for e in obs.elements if e.name == "new")
        ok, why, _ = eng.execute_decision({"action": "click", "ref": f"{new_ref},e99"}, "click", None, obs)
        assert ok and page.url.endswith("#new"), why


def test_custom_dropdown_select(browser):
    # Logged: "select raised: Locator.select_option: Error: Element is not a <select> element"
    html = """<div role="combobox" aria-label="City" tabindex="0" id="cb"
      onclick="document.getElementById('lb').style.display='block'">Choose city</div>
    <ul role="listbox" id="lb" style="display:none">
      <li role="option" onclick="cb.innerText='Dubai'; lb.style.display='none'">Dubai</li>
      <li role="option" onclick="cb.innerText='Sharjah'; lb.style.display='none'">Sharjah</li></ul>"""
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, html, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=NoModel())
        eng.do_step("select the city", "Dubai")
        assert page.locator("#cb").inner_text() == "Dubai"


def test_click_on_covered_element_recovers(browser):
    # Logged: "click raised: Locator.click: Timeout 5000ms exceeded."
    html = """<div style="position:fixed;top:0;left:0;width:100%;height:60px;background:#fff;z-index:9">bar</div>
    <button style="margin-top:10px" onclick="document.title='clicked'">Apply filters</button>"""
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, html, d)
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=NoModel())
        eng.do_step("click the apply filters button")
        assert page.title() == "clicked"


def test_cached_selector_with_hidden_duplicate_uses_the_visible_one(browser):
    # Logged: "no cached selector matched exactly one element" (mobile + desktop copies of a menu)
    html = """<nav style="display:none"><a href="#m" aria-label="Jobs">Jobs</a></nav>
    <nav><a href="#d" aria-label="Jobs">Jobs</a></nav>"""
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, html, d)
        loc = locate_cached(page, ['a[aria-label="Jobs"]'])
        assert loc is not None and loc.get_attribute("href") == "#d"


def test_model_prompt_only_gets_related_elements(browser):
    links = "".join(f'<a href="#n{i}">Nav item {i}</a>' for i in range(140))
    html = links + '<button>Save for later</button>'
    with tempfile.TemporaryDirectory() as d:
        cfg, page, cache = _setup(browser, html, d)
        obs = observe(page)
        sub = obs.subset("click save for later", 30)
        assert len(sub.elements) == 30 and any(e.name == "Save for later" for e in sub.elements)
        assert len(sub.text) < len(obs.text) / 3


def test_saved_task_replays_without_steps_and_manual_step_returns_next_elements(browser):
    from browser_engine.service import BrowserService

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.html")
        with open(path, "w") as fh:
            fh.write(FORM + '<button onclick="document.title=\'saved\'">Keep for later</button>')
        cfg = Config()
        cfg.data_dir, cfg.settle_ms, cfg.popup_wait_ms = d, 50, 0
        svc = BrowserService(cfg, local=None, claude=None, page_factory=lambda: browser.new_page())
        url = "file://" + path
        steps = [{"intent": "type into the customer name field", "value": "Guru"},
                 {"intent": "click the Medium pizza size option"}]
        r = svc.run_plan("order", steps, start_url=url, task_name="pizza order")
        assert r["ok"] and r["saved_as"] == "pizza order" and r["tiers"] == {"match": 2}
        assert "popups_closed" not in r and "claude_api_tokens" not in r  # empty fields dropped
        # replay by name, overriding a value: Claude sends ~20 tokens instead of the whole plan
        r = svc.run_plan("order", task_name="pizza order", values={"type into the customer name field": "Ana"})
        assert r["ok"] and r["tiers"] == {"cache": 2}
        assert svc.page.locator("input[name=custname]").input_value() == "Ana"
        assert "pizza order" in svc.status()["saved_tasks"]
        # manual step hands back the elements for the next one, and its refs stay valid
        els = svc.page_elements("customer name", 5)
        ref = next(l.split()[0] for l in els["elements"] if "Customer name" in l)
        t = svc.do_step("type into the customer name field", "type", ref=ref, value="Zed",
                        next_intent="click keep for later")
        assert t["ok"] and any("Keep for later" in l for l in t["page"]["elements"])
        ref2 = next(l.split()[0] for l in t["page"]["elements"] if "Keep for later" in l)
        t2 = svc.do_step("click keep for later", "click", ref=ref2)
        assert t2["ok"] and svc.page.title() == "saved", t2
