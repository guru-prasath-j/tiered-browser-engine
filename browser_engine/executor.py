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
    """First cached selector that resolves to exactly one element, else None.

    If a selector matches several elements but exactly one of them is visible (desktop + mobile
    copies of the same menu, hidden templates), that visible one is used.
    """
    fallback = None
    for s in selectors or []:
        try:
            loc = page.locator(s)
            n = loc.count()
            if n == 1:
                return loc
            if 1 < n <= 8 and fallback is None:
                visible = [i for i in range(n) if loc.nth(i).is_visible()]
                if len(visible) == 1:
                    fallback = loc.nth(visible[0])
        except Exception:
            continue
    return fallback


def settle(page, ms):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def _click(page, loc):
    """Click; if it times out (covered by a sticky bar, off-screen, animating), scroll it into
    view and retry, then fall back to a DOM click. The verifier still checks the effect.

    Returns None for a normal click, or a note on how it got through (for the log): a fallback
    usually means a popup or overlay was covering the target, and the note names it."""
    from .logviz import err_text

    try:
        loc.click(timeout=4000)
        return None
    except Exception as first:
        err = first
    try:
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=2500)
        return f"clicked after scrolling; first try: {err_text(err, 300)}"
    except Exception:
        pass
    try:
        loc.evaluate("e => e.click()")
        return f"real click failed, used a DOM click instead: {err_text(err, 300)}"
    except Exception:
        raise err


def _select(page, loc, text):
    """Native <select> by label or value; otherwise a custom dropdown: open it, pick the option."""
    is_select = False
    try:
        is_select = loc.evaluate("e => e.tagName === 'SELECT'")
    except Exception:
        pass
    if is_select:
        try:
            loc.select_option(label=text, timeout=5000)
        except Exception:
            loc.select_option(value=text, timeout=5000)
        return
    _click(page, loc)
    page.wait_for_timeout(300)
    for cand in (page.get_by_role("option", name=text, exact=True),
                 page.get_by_role("option", name=text),
                 page.get_by_role("menuitem", name=text),
                 page.get_by_text(text, exact=True)):
        try:
            for i in range(min(cand.count(), 5)):
                if cand.nth(i).is_visible():
                    _click(page, cand.nth(i))
                    return
        except Exception:
            continue
    raise ValueError(f"option {text!r} not found in the opened dropdown")


def perform(page, action, loc, text, decision, settle_ms):
    """Does the action. Returns an optional note on HOW it was done (fallbacks), for the log."""
    if action == "click":
        return _click(page, loc)
    elif action == "type":
        loc.fill(text or "", timeout=5000)
    elif action == "select":
        _select(page, loc, text)
    elif action in ("check", "uncheck"):
        want = action == "check"
        try:
            loc.set_checked(want, timeout=5000)
        except Exception:
            # hidden "sr-only" input behind a styled label, or a div acting as a radio
            loc.evaluate("(e, want) => { const i = e.tagName === 'INPUT' ? e : "
                         "e.querySelector('input[type=radio],input[type=checkbox]'); "
                         "if (i) { if (i.checked !== want) i.click(); } else e.click(); }", want)
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


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _same_number(want, got):
    """'8056708293' typed into a phone field that shows '+91 80567 08293' is a success."""
    w, g = _digits(want), _digits(got)
    return len(w) >= 6 and len(w) >= 0.8 * len(re.sub(r"\s", "", str(want or ""))) and g.endswith(w)


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


def expectation_met(page, expect):
    """(met, why). `expect` is a guess about the page after an action; empty means nothing to check."""
    expect = expect or {}
    if expect.get("url_contains") and not url_has(page.url, expect["url_contains"]):
        return False, f"URL does not contain {expect['url_contains']!r}"
    if expect.get("text_visible"):
        try:
            if not page.get_by_text(expect["text_visible"]).first.is_visible():
                return False, f"text {expect['text_visible']!r} not visible"
        except Exception:
            return False, f"text {expect['text_visible']!r} not found"
    return True, ""


def verify_ex(page, action, before, loc, text, expect, max_elements):
    """Returns (ok, reason, expect_met).

    The truth is what actually happened: the field's value, the checkbox state, a navigation or
    a change in the page. A model's guess about the URL or text afterwards (`expect`) never turns
    a real effect into a failure (that caused most false failures in the logs: models guess date
    formats and page titles wrong). It only rescues a click that changed nothing visible, and it
    is cached only when it was actually met, so wrong guesses never poison replays.
    """
    has_expect = bool(expect and (expect.get("url_contains") or expect.get("text_visible")))
    try:
        if action == "type":
            got = loc.evaluate("e => e.isContentEditable ? e.innerText : e.value")
            ok = _norm(text) in _norm(got) or _same_number(text, got)
            return ok, "ok" if ok else f"field holds {str(got)[:60]!r}, not the value", False
        if action == "select":
            got = loc.evaluate(
                "e => e.tagName === 'SELECT' ? ((e.options[e.selectedIndex] || {}).text || '') + '|' + e.value"
                " : (e.innerText || e.value || '')"
            )
            ok = _norm(text) in _norm(got)
            if not ok and has_expect:  # custom dropdowns often show the choice elsewhere
                ok = expectation_met(page, expect)[0]
            if not ok:  # or the option's text now shows somewhere near the control
                try:
                    ok = page.get_by_text(str(text), exact=True).first.is_visible()
                except Exception:
                    ok = False
            return ok, "ok" if ok else f"selected {str(got)[:60]!r}", False
        if action in ("check", "uncheck"):
            got = loc.evaluate("e => { const i = e.tagName === 'INPUT' ? e : e.querySelector('input[type=radio],input[type=checkbox]');"
                               " if (i) return i.checked; const a = e.getAttribute('aria-checked') ?? e.getAttribute('aria-selected');"
                               " return a === null ? null : a === 'true'; }")
            if got is None:  # a plain clickable div: fall back to 'did the page change'
                after = observe(page, max_elements)
                ok = after.signature != before.signature
            else:
                ok = got == (action == "check")
            return ok, "ok" if ok else "checkbox state did not change", False
        if action == "upload":
            ok = loc.evaluate("e => e.files ? e.files.length : 0") > 0
            return ok, "ok" if ok else "no file attached", False
    except Exception as e:
        from .logviz import err_text

        return False, f"could not verify: {err_text(e, 300)}", False

    if action in ("scroll", "wait", "navigate"):
        return True, "ok", False
    if action == "back":
        ok = page.url != before.url
        return ok, "ok" if ok else "URL did not change after going back", False

    # click / press / click_xy: something must actually happen.
    met, why_not = expectation_met(page, expect) if has_expect else (False, "")
    changed = page.url != before.url
    if not changed:
        changed = observe(page, max_elements).signature != before.signature
    if changed:
        return True, "ok" if (met or not has_expect) else f"ok (page changed; guess not met: {why_not})", met
    if met:
        return True, "ok (expectation met)", True
    return False, "page did not change after the action", False


def verify(page, action, before, loc, text, expect, max_elements):
    """Returns (ok, reason). See verify_ex."""
    ok, why, _ = verify_ex(page, action, before, loc, text, expect, max_elements)
    return ok, why


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
