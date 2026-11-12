"""extract_listings: read the repeated items of a page (listings, products, jobs, search results)
as compact rows, without a model and without walking interactive elements.

Sources, cheapest and most exact first:
  1. data embedded in the HTML (Next.js __NEXT_DATA__, Nuxt, JSON-LD, application/json scripts)
  2. JSON responses the page fetched while loading (the site's own data feed / XHR)
  3. repeated cards in the DOM (same tag+class siblings that carry a price or number)
The source that worked is remembered per domain in sites.json and tried first next time.

This module has no browser_engine-specific dependencies apart from a Playwright page, so other
projects (e.g. deal-watch-agents) can import it as a shared site reader.
"""
import json
import os
import re
from urllib.parse import urlparse

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
SCRIPT_JSON_RE = re.compile(r'<script[^>]+type="application/(?:ld\+)?json"[^>]*>(.*?)</script>', re.S)

TITLE_KEYS = ("title", "name", "headline", "label", "displayName")
PRICE_KEYS = ("price", "amount", "salePrice", "priceValue", "price_value", "offers")
URL_KEYS = ("url", "share_url", "shareUrl", "link", "href", "permalink", "canonicalUrl")
ID_KEYS = ("id", "listingId", "listing_id", "sku", "productId", "reference", "jobId")
USEFUL = ("bedrooms", "beds", "bathrooms", "size", "area", "location", "city", "company", "rating",
          "reviews", "date", "listed_date", "posted", "brand", "model", "year", "mileage", "salary",
          "furnished", "type", "property_type", "category", "condition")

CARDS_JS = r"""(limit) => {
  const money = /([₹$€£]|AED|USD|INR|EUR|GBP|SAR|QAR)\s?\d|\d[\d,.]*\s?(AED|USD|INR|EUR|k\b|lakh|cr)/i;
  const sig = el => el.tagName + '.' + [...el.classList].filter(c => !/\d{3,}/.test(c)).sort().join('.');
  let best = null;
  for (const parent of document.querySelectorAll('body *')) {
    const kids = [...parent.children];
    if (kids.length < 3) continue;
    const groups = {};
    for (const k of kids) { const s = sig(k); (groups[s] = groups[s] || []).push(k); }
    for (const [s, els] of Object.entries(groups)) {
      if (els.length < 3) continue;
      const priced = els.filter(e => money.test(e.innerText || ''));
      if (priced.length < Math.max(3, els.length * 0.6)) continue;
      if (!best || priced.length > best.els.length) best = {sel: s, els: priced};
    }
  }
  if (!best) return null;
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const words = e => {  // visible text nodes joined by spaces, so "Phone 0" + "₹9000" stay apart
    const out = [], w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) {
      const t = w.currentNode.textContent.trim(), p = w.currentNode.parentElement;
      if (t && (!p.checkVisibility || p.checkVisibility())) out.push(t);
    }
    return out.join(' ');
  };
  return {sig: best.sel, rows: best.els.slice(0, limit).map(e => {
    const a = e.querySelector('a[href]');
    return {text: clean(words(e)).slice(0, 220), url: a ? a.href : ''};
  }), total: best.els.length};
}"""


# ---------------------------------------------------------------------------- JSON helpers
def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, dict):
        for k in ("value", "amount", "price", "lowPrice"):
            if k in v:
                return _num(v[k])
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(\.\d+)?", v)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _has_price(item):
    for k, v in item.items():
        lk = k.lower()
        if ("price" in lk or lk in ("amount", "offers", "salary")) and (
                _num(v) is not None or (isinstance(v, dict) and any("price" in kk.lower() for kk in v))):
            return True
    return False


def _unwrap(x):
    """Items are often wrapped: {"listing_type": .., "property": {...price...}}."""
    if not isinstance(x, dict):
        return None
    if _has_price(x):
        return x
    for v in x.values():
        if isinstance(v, dict) and _has_price(v):
            return v
    return None


def _arrays(obj, path="", depth=0):
    if depth > 9:
        return
    if isinstance(obj, list):
        if len(obj) >= 3 and sum(isinstance(x, dict) for x in obj[:6]) >= 3:
            yield path, obj
        for i, v in enumerate(obj[:3]):
            yield from _arrays(v, f"{path}.{i}", depth + 1)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _arrays(v, f"{path}.{k}" if path else k, depth + 1)


def best_array(blobs):
    """(source_path, items) for the largest array of priced objects in a list of (name, json)."""
    best = None
    for name, blob in blobs:
        for path, arr in _arrays(blob):
            items = [u for u in (_unwrap(x) for x in arr) if u]
            if len(items) >= 3 and (best is None or len(items) > len(best[1])):
                best = (f"{name}:{path}", items)
    return best


def _first(item, keys):
    for k in keys:
        if k in item and item[k] not in (None, "", [], {}):
            return item[k]
    return None


def _short(v, n=60):
    if isinstance(v, dict):
        v = _first(v, ("full_name", "name", "value", "text", "label")) or next(
            (x for x in v.values() if isinstance(x, (str, int, float))), "")
    if isinstance(v, list):
        v = ", ".join(str(_short(x, 20)) for x in v[:3])
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def row_from_item(item, base_url):
    """One compact line per item: 'title | price | k=v ... | url'. Short = cheap for Claude."""
    title = _short(_first(item, TITLE_KEYS) or "", 80)
    price = _first(item, PRICE_KEYS)
    pv = _num(price)
    cur = price.get("currency") if isinstance(price, dict) else None
    period = price.get("period") if isinstance(price, dict) else None
    price_s = "" if pv is None else f"{cur + ' ' if cur else ''}{pv:,.0f}{'/' + period if period else ''}"
    extras = []
    for k in USEFUL:
        if k in item and item[k] not in (None, "", [], {}):
            extras.append(f"{k}={_short(item[k], 40)}")
        if len(extras) >= 5:
            break
    url = _first(item, URL_KEYS)
    if isinstance(url, str) and url.startswith("/"):
        p = urlparse(base_url)
        url = f"{p.scheme}://{p.netloc}{url}"
    parts = [p for p in (title, price_s, " ".join(extras)) if p]
    row = " | ".join(parts)
    if isinstance(url, str) and url:
        row += f" | {url}"
    return row, {"id": _short(_first(item, ID_KEYS) or "", 40), "price": pv}


def html_blobs(html):
    out = []
    m = NEXT_DATA_RE.search(html)
    if m:
        try:
            out.append(("next_data", json.loads(m.group(1))))
        except json.JSONDecodeError:
            pass
    for i, m in enumerate(SCRIPT_JSON_RE.finditer(html)):
        try:
            out.append((f"script{i}", json.loads(m.group(1))))
        except json.JSONDecodeError:
            pass
    return out


# ---------------------------------------------------------------------------- per-site memory
class SiteMemory:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def get(self, domain):
        return self.data.get(domain)

    def put(self, domain, source):
        self.data[domain] = {"source": source}
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
        except OSError:
            pass


# ---------------------------------------------------------------------------- main entry
def extract(page, url=None, limit=20, query="", memory=None, wait_ms=1500):
    """Read listing rows from `url` (or the current page). Returns a compact dict."""
    xhr = []

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct and resp.request.resource_type in ("xhr", "fetch"):
                body = resp.body()
                if 200 <= len(body) <= 3_000_000:
                    xhr.append((resp.url, json.loads(body)))
        except Exception:
            pass

    if url:
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
        finally:
            page.remove_listener("response", on_response)
    base = page.url
    domain = urlparse(base).netloc.lower()

    found = best_array(html_blobs(page.content()))
    source = found[0] if found else None
    if not found and xhr:
        found = best_array([(f"xhr:{urlparse(u).path}", j) for u, j in xhr])
        source = found[0] if found else None

    rows, meta = [], []
    total = 0
    if found:
        items = found[1]
        total = len(items)
        for it in items:
            r, m = row_from_item(it, base)
            rows.append(r)
            meta.append(m)
    else:
        cards = page.evaluate(CARDS_JS, max(limit * 3, 60))
        if cards:
            source = f"dom:{cards['sig']}"
            total = cards["total"]
            rows = [c["text"] + (f" | {c['url']}" if c["url"] else "") for c in cards["rows"]]

    if query:
        qw = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        rows = [r for r in rows if all(w in r.lower() for w in qw)]
    shown = rows[:limit]
    if memory is not None and source:
        memory.put(domain, source)
    out = {"url": base, "source": source, "count": len(shown), "found": total, "rows": shown}
    if not source:
        out["note"] = "no repeated items found; use get_page_elements / read steps instead"
    elif len(rows) > len(shown):
        out["more"] = f"{len(rows) - len(shown)} more rows: raise limit, or paginate before comparing"
    return out


def memory_path(data_dir):
    return os.path.join(data_dir, "sites.json")
