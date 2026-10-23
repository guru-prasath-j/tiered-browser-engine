"""Executor (runs actions with Playwright) and verifier (checks them with code, not a model)."""
import re

from .observer import observe

NEEDS_ELEMENT = {"click", "type", "select", "check", "uncheck", "upload", "extract"}

# Reads the card/row around an element: climbs from the element until the text contains a price,
# rating or star count (or gets long), without swallowing the whole page.
CARD_TEXT_JS = r"""e => {
  const clean = s => (s || '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
  let n = e, best = clean(e.innerText);
  const rich = t => /[₹$€£]\s?\d|★|\bstars?\b|\brating\b|\breviews?\b|\bforks?\b|\d\.\d\s*(out of|\/)/i.test(t);
  if (rich(best) && best.length >= 40) return best.slice(0, 800);
  for (let i = 0; i < 6 && n.parentElement; i++) {
    const t = clean(n.parentElement.innerText);
    if (t.length > 1500) break;
    n = n.parentElement; best = t;
    if (rich(t) && t.length >= 40) break;
  }
  return best.slice(0, 800);
}"""


def extract_text(loc):
    return loc.evaluate(CARD_TEXT_JS)
VALUE_ACTIONS = {"type", "select", "upload"}


def locate_cached(page, selectors):
    """First cached selector that resolves to exactly one element, else None."""
    for s in selectors or []:
        try:
            loc = page.locator(s)
            if loc.count() == 1:
                return loc
        except Exception:
            continue
    return None


def settle(page, ms):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def perform(page, action, loc, text, decision, settle_ms):
    if action == "click":
        loc.click(timeout=5000)
    elif action == "type":
        loc.fill(text or "", timeout=5000)
    elif action == "select":
        try:
            loc.select_option(label=text, timeout=5000)
        except Exception:
            loc.select_option(value=text, timeout=5000)
    elif action == "check":
        loc.check(timeout=5000)
    elif action == "uncheck":
        loc.uncheck(timeout=5000)
    elif action == "upload":
        loc.set_input_files(text, timeout=5000)
    elif action == "press":
        (loc.press(text, timeout=5000) if loc is not None else page.keyboard.press(text))
    elif action == "scroll":
        page.mouse.wheel(0, int(float(text or 700)))
    elif action == "navigate":
        page.goto(text, wait_until="domcontentloaded")
    elif action == "click_xy":
        page.mouse.click(float(decision.get("x", 0)), float(decision.get("y", 0)))
    elif action == "back":
        page.go_back(wait_until="domcontentloaded")
    elif action == "extract":
        pass  # read-only; the engine reads the text with extract_text()
    elif action == "wait":
        page.wait_for_timeout(min(int(float(text or 1500)), 10_000))
    else:
        raise ValueError(f"unknown action {action!r}")
    settle(page, settle_ms)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def verify(page, action, before, loc, text, expect, max_elements):
    """Returns (ok, reason). Element-level checks first, then 'did the page change'."""
    expect = expect or {}
    if expect.get("url_contains") and expect["url_contains"] not in page.url:
        return False, f"expected URL to contain {expect['url_contains']!r}, got {page.url}"
    if expect.get("text_visible"):
        try:
            if not page.get_by_text(expect["text_visible"]).first.is_visible():
                return False, f"expected text {expect['text_visible']!r} is not visible"
        except Exception:
            return False, f"expected text {expect['text_visible']!r} not found"

    try:
        if action == "type":
            got = loc.evaluate("e => e.isContentEditable ? e.innerText : e.value")
            ok = _norm(text) in _norm(got)
            return ok, "ok" if ok else f"field holds {str(got)[:60]!r}, not the value"
        if action == "select":
            got = loc.evaluate(
                "e => e.tagName === 'SELECT' ? ((e.options[e.selectedIndex] || {}).text || '') + '|' + e.value"
                " : (e.innerText || e.value || '')"
            )
            ok = _norm(text) in _norm(got)
            return ok, "ok" if ok else f"selected {str(got)[:60]!r}"
        if action in ("check", "uncheck"):
            ok = loc.is_checked() == (action == "check")
            return ok, "ok" if ok else "checkbox state did not change"
        if action == "upload":
            ok = loc.evaluate("e => e.files ? e.files.length : 0") > 0
            return ok, "ok" if ok else "no file attached"
    except Exception as e:
        return False, f"could not verify: {str(e).splitlines()[0][:120]}"

    if action in ("scroll", "wait", "navigate"):
        return True, "ok"
    if action == "back":
        ok = page.url != before.url
        return ok, "ok" if ok else "URL did not change after going back"

    # click / press / click_xy: something on the page must change.
    if page.url != before.url:
        return True, "ok"
    after = observe(page, max_elements)
    if after.signature != before.signature:
        return True, "ok"
    if expect.get("url_contains") or expect.get("text_visible"):
        return True, "ok (expectation met)"
    return False, "page did not change after the action"
