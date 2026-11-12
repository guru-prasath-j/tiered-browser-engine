"""choose_option / check_form on the kinds of custom questions job forms use."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser_engine import forms  # noqa: E402
from browser_engine.observer import observe  # noqa: E402

PAGE = """<!doctype html><html><head><style>
.sr{position:absolute;opacity:0;width:1px;height:1px;overflow:hidden}
.opt{cursor:pointer;display:inline-block;padding:4px;border:1px solid #ccc}
.opt.sel{background:#000;color:#fff}
</style></head><body>
<div class="q"><p>How many years of experience do you have as a Backend Engineer with Python? *</p>
  <label><input class="sr" type="radio" name="py" value="a">0 - 1 year</label>
  <label><input class="sr" type="radio" name="py" value="b">1 - 2 years</label>
  <label><input class="sr" type="radio" name="py" value="c">2 - 3 years</label>
</div>
<div class="q"><p>Are you willing to relocate to Bengaluru? *</p>
  <div id="reloc">
    <div class="opt" onclick="pick(this)">Yes</div><div class="opt" onclick="pick(this)">No</div>
  </div>
</div>
<div class="q"><span>What is your notice period? (We are looking for candidates who can join within 30 days) *</span>
  <div role="radiogroup">
    <div role="radio" aria-checked="false" tabindex="0" onclick="aria(this)">0 - 15 days</div>
    <div role="radio" aria-checked="false" tabindex="0" onclick="aria(this)">15 - 30 days</div>
    <div role="radio" aria-checked="false" tabindex="0" onclick="aria(this)">30 - 45 days</div>
  </div>
</div>
<div class="q"><label for="city">City *</label><select id="city"><option></option><option>Chennai</option><option>Bengaluru</option></select></div>
<div class="q"><label for="ctc">Current CTC *</label><input id="ctc"></div>
<script>
function pick(el){for(const o of el.parentElement.children)o.classList.remove('sel');el.classList.add('sel')}
function aria(el){for(const o of el.parentElement.children)o.setAttribute('aria-checked','false');el.setAttribute('aria-checked','true')}
</script></body></html>"""


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(PAGE)
        yield pg
        b.close()


def test_check_form_lists_everything_missing(page):
    r = forms.check_form(page)
    assert not r["complete"]
    joined = " | ".join(r["missing"])
    for q in ["Backend Engineer with Python", "relocate", "notice period", "City", "Current CTC"]:
        assert q in joined, (q, r["missing"])


def test_hidden_radio_behind_label(page):
    r = forms.choose(page, "Backend Engineer with Python", "1 - 2 years")
    assert r["ok"] and r["checked"] is True
    assert page.evaluate("document.querySelector('input[value=b]').checked")
    assert not page.evaluate("document.querySelector('input[value=a]').checked")


def test_clickable_div_option(page):
    r = forms.choose(page, "willing to relocate to Bengaluru", "Yes")
    assert r["ok"], r
    assert page.evaluate("document.querySelector('#reloc .sel').innerText") == "Yes"


def test_aria_radio_exact_beats_loose(page):
    r = forms.choose(page, "notice period", "15 - 30 days")
    assert r["ok"] and r["checked"] is True, r
    assert page.evaluate("document.querySelector('[aria-checked=true]').innerText") == "15 - 30 days"


def test_select_by_text(page):
    r = forms.choose(page, "City", "Bengaluru")
    assert r["ok"] and r["kind"] == "select"


def test_missing_option_and_question(page):
    assert not forms.choose(page, "notice period", "90 days")["ok"]
    assert not forms.choose(page, "favourite colour", "Blue")["ok"]


def test_check_form_after_answers(page):
    page.fill("#ctc", "Fixed: 5.5L")
    r = forms.check_form(page)
    assert r["complete"], r


def test_observer_lists_sr_only_radios(page):
    names = [e.name for e in observe(page).elements if e.role == "radio"]
    assert "1 - 2 years" in names


def test_answer_steps_in_a_plan_cost_no_tokens(tmp_path, page):
    os.environ["BE_DATA_DIR"] = str(tmp_path)
    from browser_engine.service import BrowserService
    pg = page
    pg.set_content(PAGE)
    if True:
        svc = BrowserService(local=None, claude=None, page_factory=lambda: pg)
        r = svc.run_plan("answer screening questions", [
            {"intent": "answer: years of experience as a Backend Engineer with Python", "value": "1 - 2 years"},
            {"intent": "answer: willing to relocate to Bengaluru", "value": "Yes"},
            {"intent": "answer: notice period", "value": "15 - 30 days"},
        ])
        assert r["ok"] and r["tiers"] == {"rule": 3} and r.get("claude_api_tokens", 0) == 0, r
        bad = svc.run_plan("x", [{"intent": "answer: notice period", "value": "6 months"}])
        assert not bad["ok"], bad
        assert svc.choose_option("City", "Chennai")["ok"]
        assert "Current CTC" in " ".join(svc.check_form()["missing"])
