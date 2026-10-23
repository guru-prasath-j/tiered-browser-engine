"""Safety rules enforced in code, before any action runs.

1. Never type secrets: password/OTP/card fields, or values that look like card numbers.
2. Irreversible actions (submit, send, pay, delete, post...) need explicit confirmation,
   which the caller should only give after the user approved it in chat.
"""
import re

SENSITIVE = re.compile(
    r"pass(word|code|phrase)|\botp\b|one[- ]?time|\bcvv\b|\bcvc\b|security code|card ?number|"
    r"credit card|debit card|\bpin\b|aadhaa?r|\bssn\b|passport|account number|api[ _-]?key|secret|token",
    re.I,
)
SECRET_AUTOCOMPLETE = re.compile(r"^(cc-|one-time-code|current-password|new-password)", re.I)
CARD_LIKE = re.compile(r"^\D*(\d[ -]?){13,19}\D*$")

IRREVERSIBLE = re.compile(
    r"\b(submit|send|pay|payment|purchase|buy|place order|checkout|check out|confirm|delete|remove|"
    r"post|publish|transfer|book now|subscribe|sign up|register|unsubscribe|easy apply|"
    r"apply (now|for|to))\b",
    re.I,
)

DESCRIBE_JS = """e => ({
  type: (e.getAttribute('type') || '').toLowerCase(),
  autocomplete: e.getAttribute('autocomplete') || '',
  label: [e.getAttribute('aria-label'), e.getAttribute('name'), e.getAttribute('placeholder'),
          e.innerText, e.tagName === 'INPUT' ? e.value : ''].filter(Boolean).join(' ').slice(0, 120)
})"""


class Blocked(Exception):
    """Raised instead of performing an unsafe action. Never retried by other tiers."""


def describe(loc):
    if loc is None:
        return {}
    try:
        return loc.evaluate(DESCRIBE_JS)
    except Exception:
        return {}


class Guard:
    def __init__(self, confirm_irreversible=False):
        self.confirm_irreversible = confirm_irreversible

    def check(self, intent, action, text, el):
        el = el or {}
        label = el.get("label", "")
        if action in ("type", "select"):
            if el.get("type") == "password" or SECRET_AUTOCOMPLETE.match(el.get("autocomplete", "")):
                raise Blocked("refusing to type into a password / payment / OTP field; the user must do it")
            if SENSITIVE.search(intent) or SENSITIVE.search(label):
                raise Blocked(f"refusing to fill a sensitive field ({label or intent!r}); the user must do it")
            if text and CARD_LIKE.match(str(text)):
                raise Blocked("refusing to type a value that looks like a card or account number")
        if action in ("click", "press", "click_xy") and not self.confirm_irreversible:
            hit = IRREVERSIBLE.search(intent) or IRREVERSIBLE.search(label)
            if hit:
                raise Blocked(
                    f"'{hit.group(0)}' looks irreversible; ask the user, then retry with "
                    "confirm_irreversible=true"
                )
