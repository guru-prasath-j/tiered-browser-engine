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


# Actions whose result is read straight off the element. For these the element's own state is
# the truth; a model's guess about the URL or visible text afterwards is ignored.
ELEMENT_CHECKED = {"type", "select", "check", "uncheck", "upload"}


def url_has(url, part):
    """Case-insensitive, and a space in the guess may be '_', '-', '+' or '%20' in the URL."""
    u = str(url or "").lower()
    p = str(part or "").strip().lower()
    return any(v in u for v in {p, p.replace(" ", "_"), p.replace(" ", "-"), p.replace(" ", "+"),
                                 p.replace(" ", "%20")})


def verify(page, action, before, loc, text, expect, max_elements):
    """Returns (ok, reason). Element-level checks first, then 'did the page change'."""
    expect = {} if action in ELEMENT_CHECKED else (expect or {})
    if expect.get("url_contains") and not url_has(page.url, expect["url_contains"]):
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


# ---- wrong-target guard ------------------------------------------------------
# "Did the page change?" can't tell a right click from a wrong one: clicking any link changes the
# page. When the intent names its target by label ("click the new link") and an element with
# exactly that label is on the page, a click on an element whose label shares no word with the
# intent is almost certainly the model picking the wrong ref, so it is rejected and retried.
TARGET_FILLER = {
    "click", "tap", "press", "open", "select", "choose", "go", "hit", "follow", "the", "a", "an", "on",
    "in", "into", "of", "to", "at", "from", "for", "with", "its", "that", "this", "link", "button",
    "tab", "menu", "item", "option", "icon", "bar", "top", "bottom", "nav", "navigation", "header",
    "footer", "sidebar", "side", "left", "right", "page", "category", "section", "label", "labelled",
    "labeled", "called", "named", "entry", "card", "row",
}
GENERIC = {"first", "second", "third", "last", "next", "previous", "result", "results", "listing",
           "one", "any"}


def _words(s):
    return re.findall(r"[a-z0-9]+", str(s or "").lower())


def wrong_target(intent, chosen, elements):
    """(chosen, better) if a clearly better-labelled element exists, else None."""
    words = [w for w in _words(intent) if w not in TARGET_FILLER]
    named = [w for w in words if w not in GENERIC]
    if not named or chosen is None:
        return None
    if set(_words(chosen.name)) & set(words):
        return None
    phrases = {" ".join(named[i:j]) for i in range(len(named)) for j in range(i + 1, len(named) + 1)}
    for e in elements:
        if e is not chosen and " ".join(_words(e.name)) in phrases:
            return chosen, e
    return None
