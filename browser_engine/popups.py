"""Close optional popups with plain DOM clicks, and remember per site which button worked.

Popups that sit on top of the page (cookie banners, Google One Tap "Sign in with Google",
"Sign in to save your search", "Get the app" banners, newsletter boxes) make clicks fail, which
sends the step up to a model for nothing. This module removes them for 0 tokens:

* Google One Tap is removed from the DOM outright.
* For every visible overlay/dialog, the button whose label means "close" is clicked
  (close, x, no thanks, not now, reject all, accept, got it...). Nothing here signs in,
  subscribes, buys or submits.
* The label that worked is stored per site in popups.json and tried first next time.

It only runs when a new page has loaded (URL changed) or when a cached step failed, so a dialog
that the plan itself opened (a filter panel, a date picker) is not closed in the middle of a task.
"""
import json
import os
from urllib.parse import urlparse

# Labels that only ever CLOSE something. Order = preference (decline before accept).
DISMISS_LABELS = [
    "close", "close dialog", "close modal", "close popup", "close banner", "dismiss", "x", "×", "✕", "✖",
    "no thanks", "no, thanks", "no thank you", "not now", "maybe later", "later", "skip", "skip for now",
    "reject all", "reject", "decline", "decline all", "necessary only", "only necessary",
    "use necessary cookies only", "accept all", "accept", "accept cookies", "allow all", "i agree",
    "agree", "got it", "ok", "okay", "continue to site", "stay on web", "continue in browser",
]

# Extra labels for guests only: they are never signed in, so "continue as guest" is always right.
# The owner may want to stay on the sign-in path, so these are not clicked in owner mode.
GUEST_LABELS = ["continue as guest", "continue without signing in", "continue without account",
                "stay signed out", "use without an account", "browse as guest"]

POPUP_JS = r"""
([labels, preferred]) => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  let removed = 0;
  document.querySelectorAll('#credential_picker_container, #credential_picker_iframe, iframe[src*="accounts.google.com/gsi"]')
    .forEach(e => { e.remove(); removed++; });
  const isOverlay = e => {
    if (!vis(e)) return false;
    if (e.matches('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]')) return true;
    for (let n = e; n && n !== document.body; n = n.parentElement) {
      const p = getComputedStyle(n).position;
      if (p === 'fixed' || p === 'sticky') return true;
    }
    return false;
  };
  const overlays = Array.from(document.querySelectorAll(
    '[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open],' +
    '[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=onetrust i],' +
    '[class*=modal i],[class*=popup i],[class*=overlay i],[class*=banner i],[class*=interstitial i]'
  )).filter(isOverlay);
  const wanted = preferred.filter(l => labels.includes(l)).concat(labels);
  const clicked = [];
  const done = new Set();
  for (const box of overlays) {
    if (Array.from(done).some(d => d.contains(box) || box.contains(d))) continue;
    const btns = Array.from(box.querySelectorAll(
      'button,[role=button],a,input[type=button],input[type=submit],[aria-label]')).filter(vis);
    let best = null, rank = 1e9;
    for (const b of btns) {
      const names = [b.getAttribute('aria-label'), b.getAttribute('title'), b.innerText, b.value]
        .map(norm).filter(Boolean);
      for (const n of names) { const i = wanted.indexOf(n); if (i >= 0 && i < rank) { best = b; rank = i; } }
    }
    if (best) { try { best.click(); clicked.push(wanted[rank]); done.add(box); } catch (e) {} }
  }
  return {removed, clicked};
}
"""


class PopupMemory:
    """popups.json: {domain: {label: times it worked}}. Best label is tried first next time."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except Exception:
            self.data = {}

    def preferred(self, domain):
        return list(self.data.get(domain, {}).keys())

    def learn(self, domain, labels):
        if not labels or not domain:
            return
        site = self.data.setdefault(domain, {})
        for l in labels:
            site[l] = site.get(l, 0) + 1
        self.data[domain] = dict(sorted(site.items(), key=lambda kv: -kv[1])[:8])
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass  # memory is a speed-up, never a reason to fail a step

    def list(self):
        return self.data

    def clear(self, domain=None):
        if domain:
            self.data.pop(domain, None)
        else:
            self.data = {}
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
        except OSError:
            pass


def dismiss(page, memory=None, guest=False, rounds=2):
    """Close optional popups. Returns {"clicked": [labels], "removed_google_one_tap": n}."""
    try:
        domain = (urlparse(page.url).hostname or "").lower()
    except Exception:
        return {"clicked": [], "removed_google_one_tap": 0}
    labels = (GUEST_LABELS + DISMISS_LABELS) if guest else DISMISS_LABELS
    preferred = memory.preferred(domain) if memory else []
    clicked, removed = [], 0
    for _ in range(rounds):  # some sites stack two popups (cookies, then "sign in")
        try:
            r = page.evaluate(POPUP_JS, [labels, preferred])
        except Exception:
            break
        removed += r.get("removed", 0)
        if not r.get("clicked"):
            break
        clicked += r["clicked"]
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    if memory and clicked:
        memory.learn(domain, clicked)
    return {"clicked": clicked, "removed_google_one_tap": removed}
