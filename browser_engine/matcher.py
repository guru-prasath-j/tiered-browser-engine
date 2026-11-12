"""Tier "match": solve a step from the element labels alone, with no model (0 tokens).

Most plan steps name their target by its visible label ("type into the email field",
"click the Next page link", "click the Medium pizza size option"). If exactly one element
of a compatible role carries that label, there is nothing for a model to decide.
The match is still executed through the normal guarded path (safety guard, wrong-target guard,
effect verification), and it is cached like any other learned action.

Anything ambiguous returns None and the step goes to the local model as before.
"""
import re

ORDINAL = {"first", "second", "third", "fourth", "fifth", "last", "1st", "2nd", "3rd", "any", "random"}
FILLER = {
    "click", "tap", "press", "open", "select", "choose", "pick", "go", "hit", "follow", "type", "enter",
    "fill", "input", "write", "put", "check", "tick", "uncheck", "untick", "enable", "disable", "toggle",
    "the", "a", "an", "on", "in", "into", "of", "to", "at", "from", "for", "with", "its", "that", "this",
    "and", "or", "by", "out", "up", "my", "your", "our",
    "link", "button", "btn", "tab", "menu", "item", "option", "icon", "bar", "nav", "navigation",
    "header", "footer", "sidebar", "side", "left", "right", "page", "category", "section", "label",
    "labelled", "labeled", "called", "named", "entry", "card", "row", "field", "box", "textbox",
    "input", "dropdown", "drop", "down", "list", "checkbox", "radio", "control", "area", "form", "value",
}
ROLES = {
    "type": {"textbox", "searchbox", "combobox"},
    "select": {"combobox", "listbox"},
    "check": {"checkbox", "radio", "switch"},
    "uncheck": {"checkbox", "switch"},
    "click": {"link", "button", "tab", "menuitem", "option", "radio", "checkbox", "switch"},
}
KEYS = {"enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
        "space": "Space", "down": "ArrowDown", "up": "ArrowUp"}

VERB = [
    (re.compile(r"^(type|enter|fill|input|write|put|search for)\b"), "type"),
    (re.compile(r"^(select|choose|pick)\b"), "select"),
    (re.compile(r"^(uncheck|untick|deselect|disable)\b"), "uncheck"),
    (re.compile(r"^(check|tick|enable)\b"), "check"),
    (re.compile(r"^(click|tap|press|hit|open|follow|go to|choose|select)\b"), "click"),
]
KEY_RE = re.compile(r"^(press|hit)\s+(the\s+)?(enter|return|tab|escape|esc|space)(\s+key)?\b")
BACK_RE = re.compile(r"^(go|navigate)\s+back\b|^back\b")
SCROLL_RE = re.compile(r"^scroll(\s+(the\s+page\s+)?(down|up))?\b")
URL_RE = re.compile(r"https?://\S+")


def _stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def words(s):
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", str(s or "").lower())]


def _decision(action, ref=None, text=None, why=""):
    return {"action": action, "ref": ref, "text": text, "expect": {}, "reason": f"label match: {why}"[:80]}


def match(intent, value, obs):
    """A decision dict like the models return, or None when the step is not obvious."""
    it = re.sub(r"\s+", " ", str(intent or "").strip().lower())
    if not it or it.startswith(("read", "extract", "answer", "wait", "verify", "find", "look")):
        return None

    m = KEY_RE.match(it)
    if m:
        return _decision("press", None, KEYS[m.group(3)], m.group(3))
    if BACK_RE.match(it):
        return _decision("back", why="back")
    m = SCROLL_RE.match(it)
    if m and len(it.split()) <= 4:
        return _decision("scroll", text="-700" if (m.group(3) == "up") else "700", why="scroll")
    if re.match(r"^(open|go to|navigate to|visit)\b", it):
        url = value if value and str(value).startswith("http") else (URL_RE.search(intent or "") or [None])[0]
        if url:
            return _decision("navigate", text=url, why="url")

    action = next((a for rx, a in VERB if rx.match(it)), None)
    if action is None:
        return None
    if action in ("type", "select") and value is None:
        action = "click" if action == "select" else None
        if action is None:
            return None
    iw = words(it)
    if set(iw) & ORDINAL:
        return None  # "the first result" is about position, not a label: leave it to a model
    target = [w for w in iw if w not in FILLER]
    tset = set(target)
    roles = ROLES[action]
    phrase = " ".join(target)

    scored = []
    for e in obs.elements:
        if e.role not in roles:
            continue
        nw = words(e.name)
        if not nw:
            continue
        nset = set(nw)
        overlap = len(nset & tset)
        if not overlap:
            continue
        score = overlap / max(len(tset), 1)
        if nset <= tset:            # the element's whole label is named in the intent
            score += 0.5
        if " ".join(nw) == phrase:  # exact label
            score += 0.5
        if e.in_view:
            score += 0.05
        scored.append((score, e))

    if not scored and action == "type":
        boxes = [e for e in obs.elements if e.role in roles and e.in_view]
        if len(boxes) == 1:  # the only text field on screen
            return _decision("type", boxes[0].ref, value, "only text field")
        return None
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    best, el = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best < 0.5 or best - second < 0.34:
        return None  # two candidates look alike: let a model look at the page
    if action == "select" and el.role not in ROLES["select"]:
        return None
    text = value if action in ("type", "select") else None
    return _decision(action, el.ref, text, el.name[:40])
