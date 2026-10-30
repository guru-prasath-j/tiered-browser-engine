"""Guest mode: the browser that remote callers (A2A over Tailscale) get.

Remote callers must never act as the owner. So guest mode:

1. Runs in its OWN Chromium with a fresh, empty context (no cookies, no logins, no history).
   It never touches the owner's Chrome profile on CDP :9222. The context is thrown away after
   every task.
2. Blocks private / local network targets (localhost, 192.168.x, 10.x, Tailscale 100.x, *.local,
   file://, chrome://) at the network layer, so the agent can't be used to reach the owner's LAN.
3. Closes optional popups (Google One Tap, cookie banners, "sign in to save", app banners) with
   plain DOM clicks, no model call, and remembers per site which button worked.
4. Stops with `login_required` when a page really needs a login (redirect to a sign-in page, or a
   password box still showing after popups were closed). It never signs in.
5. Never confirms irreversible actions (submit, pay, send, book, apply...) and never waits for a
   human to solve a captcha: it reports the bot check instead.
"""
import ipaddress
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

from .engine import Engine, HumanNeeded
from . import human
from . import popups as popup_mod
from .safety import Blocked, Guard
from .service import BrowserService


class LoginRequired(Blocked):
    """The page needs a login. Guests are never signed in, so the task stops here."""


# ---- 1. URL / network policy -------------------------------------------------
PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".ts.net", ".home.arpa")


def _env_domains(name):
    return [d.strip().lower().lstrip(".") for d in os.getenv(name, "").split(",") if d.strip()]


def _matches(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def _ip_private(ip):
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")))


class UrlPolicy:
    """Decides which hosts a guest may load. Results are cached per host."""

    def __init__(self, resolve=True):
        self.resolve = resolve
        self.block = _env_domains("BE_GUEST_BLOCK_DOMAINS")
        self.allow = _env_domains("BE_GUEST_ALLOW_DOMAINS")  # if set, ONLY these (and subdomains)
        self._seen = {}

    def why_blocked(self, url):
        """None if allowed, else a short reason."""
        p = urlparse(url or "")
        if p.scheme in ("data", "blob", "about"):
            return None  # in-page resources, no network target
        if p.scheme not in ("http", "https"):
            return f"scheme '{p.scheme}:' is not allowed for remote users"
        host = (p.hostname or "").lower().rstrip(".")
        if not host:
            return "no host in URL"
        if host in self._seen:
            return self._seen[host]
        reason = self._check_host(host)
        self._seen[host] = reason
        return reason

    def _check_host(self, host):
        if host == "localhost" or host.endswith(PRIVATE_SUFFIXES) or "." not in host:
            return f"{host} is a local/private address"
        if _matches(host, self.block):
            return f"{host} is blocked for remote users"
        if self.allow and not _matches(host, self.allow):
            return f"{host} is not on the allowed-sites list"
        try:
            ipaddress.ip_address(host)
            return f"{host} is a private address" if _ip_private(host) else None
        except ValueError:
            pass
        if self.resolve:
            try:
                for info in socket.getaddrinfo(host, None):
                    if _ip_private(info[4][0]):
                        return f"{host} resolves to a private address"
            except socket.gaierror:
                return None  # let the browser show its own DNS error
        return None


# ---- 4. login walls ----------------------------------------------------------
LOGIN_HOSTS = re.compile(r"^(accounts\.google\.|login\.microsoftonline\.|login\.live\.|appleid\.apple\.|"
                         r"auth\.|login\.|signin\.|sso\.|id\.)", re.I)
LOGIN_PATHS = re.compile(r"/(log-?in|sign-?in|signin|auth(enticate)?|sso|oauth2?|session/new|account/login|"
                         r"ap/signin|users/sign_in)(/|$|\?)", re.I)

PASSWORD_JS = r"""
() => Array.from(document.querySelectorAll('input[type=password]')).some(e => {
  const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
  return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
})
"""


def login_wall(page):
    """Short reason if this page requires signing in, else None. Call after popups.dismiss."""
    p = urlparse(page.url or "")
    host = (p.hostname or "").lower()
    if LOGIN_HOSTS.match(host) or LOGIN_PATHS.search(p.path or ""):
        return f"{host} asks for a login"
    try:
        if page.evaluate(PASSWORD_JS):
            return f"{host} shows a password box"
    except Exception:
        pass
    return None


# ---- engine + service ----------------------------------------------------------
class GuestEngine(Engine):
    guest_popups = True

    def human_gate(self):
        # Nobody can solve a captcha for a remote guest: report it right away, no notification.
        reason = human.detect(self.page)
        if reason:
            raise HumanNeeded(reason, self.target_url)

    def before_step(self):
        super().before_step()  # closes popups on new pages
        wall = login_wall(self.page)
        if wall:
            raise LoginRequired(wall)


GUEST_NEXT = {
    "login_required": "This site needs a login. Remote users run without any account, so this task "
                      "can't be done here. Tell the user to run it on their own machine.",
    "blocked": "A safety rule stopped this step (it looked like submit/pay/send/book, a password or card "
               "field, or a private address). Remote users can't approve it. Tell the user what was blocked.",
    "needs_human": "The site showed a bot check (captcha). Remote users can't solve it. Tell the user to "
                   "try again later or run it on their own machine.",
    "failed": "One step failed. You may call get_page_elements, then do_browser_step once for that step, "
              "then run_browser_plan with the remaining steps. Give up after two tries.",
}


class GuestBrowserService(BrowserService):
    """BrowserService for remote callers: isolated browser, no logins, fail-closed rules."""

    def __init__(self, cfg=None, headless=None, **kw):
        super().__init__(cfg, claude=None, **kw)
        self._claude = None  # the paid Claude API tier is never used for guests
        self.cfg.human_wait_s = 0
        self.headless = (os.getenv("BE_GUEST_HEADLESS", "1") != "0") if headless is None else headless
        self.policy = UrlPolicy()
        if self.popups is None:  # guests always get popup handling
            self.popups = popup_mod.PopupMemory(self.cfg.popups_path)
        self._browser = None
        self._ctx = None

    # One fresh context per task; one Chromium process kept warm.
    def _get_page(self):
        if self._page_alive():
            return self.page
        self.page = None
        self.last_obs = None
        if self.page_factory:
            self.page = self.page_factory()
        else:
            if self._browser is None or not self._browser.is_connected():
                from playwright.sync_api import sync_playwright

                if self._pw is None:
                    self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=self.headless)
            self._ctx = self._browser.new_context(viewport={"width": 1280, "height": 800},
                                                  accept_downloads=False)
            self._ctx.route("**/*", self._route)
            self.page = self._ctx.new_page()
        return self.page

    def _route(self, route):
        if self.policy.why_blocked(route.request.url):
            return route.abort("blockedbyclient")
        return route.continue_()

    def reset(self):
        """Forget everything from the previous task (cookies, storage, tab)."""
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:
            pass
        self._ctx = None
        self.page = None
        self.last_obs = None

    def _engine(self, page, log, confirm_irreversible):
        return GuestEngine(page, self.cfg, self.cache, log, local=self._get_local(), claude=None,
                           guard=Guard(False), popups=self.popups)

    # ---- tools (guest versions) --------------------------------------------
    def run_plan(self, goal, steps, start_url=None, confirm_irreversible=False):
        if start_url:
            why = self.policy.why_blocked(start_url)
            if why:
                return {"ok": False, "blocked": True, "error": why, "next": GUEST_NEXT["blocked"]}
        try:
            out = super().run_plan(goal, steps, start_url, False)
        except LoginRequired as e:  # raised outside a step (shouldn't happen, but fail closed)
            out = {"ok": False, "blocked": True, "error": str(e)}
        return self._guest_result(out)

    def _guest_result(self, out):
        if out.get("ok"):
            return out
        err = str(out.get("error", ""))
        if out.get("needs_human"):
            kind = "needs_human"
            out.pop("remaining_steps", None)
            out.pop("resume_url", None)
        elif out.get("blocked") and ("login" in err or "password box" in err):
            kind = "login_required"
            out["login_required"] = True
            out.pop("page", None)  # nothing useful to retry on a login page
        elif out.get("blocked"):
            kind = "blocked"
        else:
            kind = "failed"
        out["next"] = GUEST_NEXT[kind]
        return out

    def page_elements(self, query="", limit=30):
        page = self._get_page()
        popup_mod.dismiss(page, self.popups, guest=True)
        wall = login_wall(page)
        if wall:
            return {"ok": False, "login_required": True, "error": wall, "next": GUEST_NEXT["login_required"]}
        return super().page_elements(query, min(int(limit), 40))

    def do_step(self, intent, action, ref=None, value=None, text=None, confirm_irreversible=False):
        action = (action or "").lower().strip()
        if action == "navigate":
            why = self.policy.why_blocked(value or "")
            if why:
                return {"ok": False, "blocked": True, "error": why, "next": GUEST_NEXT["blocked"]}
        if action == "upload":
            return {"ok": False, "blocked": True, "error": "file uploads are not allowed for remote users"}
        return super().do_step(intent, action, ref, value, text, False)

    def wait_human(self, *a, **kw):
        return {"resolved": False, "next": GUEST_NEXT["needs_human"]}

    def cache_op(self, op="list", domain=None):
        return {"error": "not available to remote users"}


class Busy:
    """One guest task at a time. A stuck holder is released after `stale_s`."""

    def __init__(self, stale_s=600):
        self.stale_s = stale_s
        self.owner = None
        self.since = 0.0
        self._m = threading.Lock()

    def acquire(self, owner):
        with self._m:
            if self.owner and self.owner != owner and time.time() - self.since < self.stale_s:
                return False
            self.owner, self.since = owner, time.time()
            return True

    def release(self, owner):
        with self._m:
            if self.owner == owner:
                self.owner = None
