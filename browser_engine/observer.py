"""Page observer: turns a page into a compact, text-only list of interactive elements.

This replaces screenshots. Each element gets a short ref (e0, e1, ...) the model can
point at, plus a list of stable selector candidates the action cache can replay later.
"""
import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

EXTRACT_JS = r"""
(maxEls) => {
  for (const e of document.querySelectorAll('[data-be-ref]')) e.removeAttribute('data-be-ref');
  const SEL = 'h1,h2,h3,a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=checkbox],[role=radio],[role=combobox],[role=option],[role=switch],' +
    '[role=textbox],[role=searchbox],[contenteditable=""],[contenteditable="true"],[onclick],' +
    '[tabindex]:not([tabindex="-1"])';
  const ARIA = new Set(['heading','link','button','combobox','textbox','checkbox','radio','searchbox','tab','menuitem','option','switch']);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const q = s => JSON.stringify(s);
  const roleOf = el => {
    const r = el.getAttribute('role'); if (r) return r;
    const t = el.tagName.toLowerCase();
    if (/^h[1-3]$/.test(t)) return 'heading';
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button','submit','reset','image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'file') return 'file';
      if (ty === 'search') return 'searchbox';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  };
  const nameOf = el => {
    const al = el.getAttribute('aria-label'); if (clean(al)) return clean(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l && clean(l.innerText)) return clean(l.innerText);
      } catch (e) {}
    }
    const pl = el.closest('label');
    if (pl && pl !== el && clean(pl.innerText)) return clean(pl.innerText);
    for (const a of ['placeholder','title','alt']) { const v = el.getAttribute(a); if (clean(v)) return clean(v); }
    if (el.tagName === 'INPUT' && ['button','submit','reset'].includes((el.type || '').toLowerCase())) return clean(el.value);
    return clean(el.innerText || el.textContent);
  };
  const dynamicId = id => /\d{3,}|[a-f0-9]{10,}|^:|^(ember|react|mui|radix)/i.test(id);
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden' || el.disabled || el.getAttribute('aria-hidden') === 'true') continue;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    const hidden = r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0';
    if (hidden && type !== 'file') continue;
    const role = roleOf(el);
    const name = nameOf(el);
    if (role === 'generic' && !name) continue;
    const sels = [];
    if (el.id && !dynamicId(el.id)) sels.push('#' + CSS.escape(el.id));
    for (const a of ['data-testid','data-test','data-qa','data-cy']) {
      const v = el.getAttribute(a); if (v) sels.push('[' + a + '=' + q(v) + ']');
    }
    const nm = el.getAttribute('name'); if (nm) sels.push(tag + '[name=' + q(nm) + ']');
    const al = el.getAttribute('aria-label'); if (al) sels.push(tag + '[aria-label=' + q(al) + ']');
    const ph = el.getAttribute('placeholder'); if (ph) sels.push(tag + '[placeholder=' + q(ph) + ']');
    if (name && ARIA.has(role)) sels.push('role=' + role + '[name=' + q(name) + ']');
    if (name && (role === 'link' || role === 'button')) sels.push('text=' + q(name));
    let value = '';
    if (type === 'password') value = el.value ? '***' : '';
    else if (tag === 'select') value = clean((el.options[el.selectedIndex] || {}).text || '');
    else if ((tag === 'input' || tag === 'textarea') && role !== 'button' && !['checkbox','radio','file'].includes(type)) value = clean(String(el.value || ''));
    else if (el.isContentEditable) value = clean(el.innerText);
    const states = [];
    for (const a of ['aria-expanded','aria-pressed','aria-selected']) {
      const v = el.getAttribute(a); if (v) states.push(a.slice(5) + '=' + v);
    }
    const ref = 'e' + (i++);
    el.setAttribute('data-be-ref', ref);
    out.push({ref, role, name, tag, value, checked: el.checked === true,
              inView: r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw,
              state: states.join(' '), sels});
    if (out.length >= maxEls * 3) break;
  }
  const els = out.filter(e => e.inView).concat(out.filter(e => !e.inView)).slice(0, maxEls);
  const heads = Array.from(document.querySelectorAll('h1,h2,[role=alert],[role=dialog] [role=heading]'))
    .map(h => clean(h.innerText)).filter(Boolean).slice(0, 6);
  return {url: location.href, title: document.title, heads, els};
}
"""


@dataclass
class Element:
    ref: str
    role: str
    name: str
    tag: str
    value: str = ""
    checked: bool = False
    in_view: bool = True
    state: str = ""
    selectors: list = field(default_factory=list)

    def line(self):
        s = f'{self.ref} {self.role} "{self.name}"'
        if self.value:
            s += f' value="{self.value}"'
        if self.checked:
            s += " [checked]"
        if self.state:
            s += f" [{self.state}]"
        if not self.in_view:
            s += " (offscreen)"
        return s


@dataclass
class Observation:
    url: str
    title: str
    headings: list
    elements: list

    def __post_init__(self):
        self.by_ref = {e.ref: e for e in self.elements}

    @property
    def text(self):
        parts = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.headings:
            parts.append("Headings: " + " | ".join(self.headings))
        parts.append("Elements:")
        parts.extend(e.line() for e in self.elements)
        return "\n".join(parts)

    @property
    def signature(self):
        body = "\n".join([self.url, self.title, *self.headings, *(e.line() for e in self.elements)])
        return hashlib.sha1(body.encode()).hexdigest()


def observe(page, max_elements=150):
    try:
        data = page.evaluate(EXTRACT_JS, max_elements)
    except Exception:
        # Usually "execution context destroyed" mid-navigation: wait and retry once.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        data = page.evaluate(EXTRACT_JS, max_elements)
    els = [
        Element(
            ref=e["ref"], role=e["role"], name=e["name"], tag=e["tag"], value=e["value"],
            checked=e["checked"], in_view=e["inView"], state=e["state"], selectors=e["sels"],
        )
        for e in data["els"]
    ]
    return Observation(url=data["url"], title=data["title"], headings=data["heads"], elements=els)


_ID_SEG = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f-]{32,36}|.*\d{4,}.*)$", re.I)


def page_key(url):
    """(domain, normalized path) so /jobs/48213 and /jobs/99102 share cache entries."""
    p = urlparse(url)
    segs = [(":id" if _ID_SEG.match(s) else s) for s in p.path.lower().split("/") if s]
    return p.netloc.lower(), "/" + "/".join(segs)
