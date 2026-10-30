"""Human handoff for bot checks ("Are you a robot?", press-and-hold, reCAPTCHA, Cloudflare).

The engine never tries to solve these. It detects them, brings the Chrome tab to the front,
pings the user with a macOS notification, and polls until the page moves on. Then the plan
resumes from the step it stopped at.
"""
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

URL_MARKERS = re.compile(
    r"captcha|/sttc/px/|/cdn-cgi/challenge|challenges\.cloudflare\.com|/sorry/index|"
    r"/distil_r_captcha|/_incapsula_|/blocked\?|perimeterx|datadome", re.I)

TEXT_MARKERS = re.compile(
    r"are you a (person or a )?robot|verify (that )?you are (a )?human|i'?m not a robot|"
    r"press (&|and) hold|unusual traffic from your computer|checking (if the site connection is secure|your browser)|"
    r"complete the (security check|captcha)|confirm you are human|prove you'?re not a robot|"
    r"verify you are not a bot|human verification", re.I)

PROBE_JS = r"""
() => {
  const t = (document.body ? document.body.innerText : '').slice(0, 1500);
  const frames = Array.from(document.querySelectorAll('iframe')).filter(f => {
    const r = f.getBoundingClientRect();
    return r.width > 60 && r.height > 60;   // ignore invisible/background recaptcha v3 frames
  }).map(f => f.src || '').join(' ');
  return {title: document.title || '', text: t, frames};
}
"""

FRAME_MARKERS = re.compile(r"recaptcha/(api2|enterprise)/(anchor|bframe)|hcaptcha\.com/captcha|"
                           r"challenges\.cloudflare\.com|captcha-delivery\.com", re.I)


def detect(page):
    """Return a short reason string if the page is a bot check, else None."""
    try:
        url = page.url or ""
    except Exception:
        return None
    if URL_MARKERS.search(url.split("?")[0]):
        return f"bot check page ({urlparse(url).netloc})"
    try:
        p = page.evaluate(PROBE_JS)
    except Exception:
        return None
    m = TEXT_MARKERS.search(p.get("title", "") + "\n" + p.get("text", ""))
    if m:
        return f'bot check: "{m.group(0)}"'
    if p.get("title", "").strip().lower() == "just a moment...":
        return "bot check: Cloudflare 'Just a moment'"
    if FRAME_MARKERS.search(p.get("frames", "")):
        return "bot check: captcha widget on page"
    return None


def notify(page, reason):
    """Bring the tab forward and ping the user. Best effort; never raises."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    msg = f"{reason[:90]}. Solve it in Chrome; I'll continue automatically."
    if sys.platform == "darwin":
        script = f'display notification {_q(msg)} with title "browser-engine needs you" sound name "Glass"'
        try:
            subprocess.run(["osascript", "-e", script], timeout=3, capture_output=True)
            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                           timeout=3, capture_output=True)
        except Exception:
            pass
    print(f"[browser-engine] HUMAN NEEDED: {msg}", file=sys.stderr)


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wait_until_clear(page, timeout_s, poll_s=1.5):
    """Poll until the bot check is gone for two polls in a row. True if cleared in time."""
    deadline = time.time() + max(0, timeout_s)
    clear_streak = 0
    while time.time() < deadline:
        if detect(page) is None:
            clear_streak += 1
            if clear_streak >= 2:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                return True
        else:
            clear_streak = 0
        time.sleep(poll_s)
    return detect(page) is None


def same_target(current, target):
    a, b = urlparse(current or ""), urlparse(target or "")
    return a.netloc == b.netloc and a.path.rstrip("/") == b.path.rstrip("/")
