"""extract_listings: embedded JSON, the page's own XHR feed, and DOM cards, with no model."""
import json
import os
import tempfile

import pytest

from browser_engine import listings


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        p = b.new_page()
        yield p
        b.close()


def _item(i, price):
    return {"listing_type": "property", "property": {
        "id": str(i), "title": f"Flat {i}", "price": {"value": price, "currency": "AED", "period": "yearly"},
        "bedrooms": "1", "size": {"value": 800, "unit": "sqft"},
        "location": {"full_name": "Marina 101, Dubai Marina"}, "share_url": f"/en/plp/{i}.html"}}


def _serve(page, url, html, routes=None):
    def handler(route):
        u = route.request.url
        for pat, (ct, body) in (routes or {}).items():
            if pat in u:
                return route.fulfill(status=200, content_type=ct, body=body)
        return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
    page.unroute("**/*")
    page.route("**/*", handler)


def test_next_data_rows_are_compact(page):
    data = {"props": {"pageProps": {"searchResult": {"listings": [
        {"listing_type": "project", "project": {}}] + [_item(i, 100000 + i * 1000) for i in range(25)]}}}}
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'
    _serve(page, "https://site.example/", html)
    with tempfile.TemporaryDirectory() as d:
        mem = listings.SiteMemory(os.path.join(d, "sites.json"))
        r = listings.extract(page, "https://site.example/rent/marina.html", limit=10, memory=mem)
        assert r["source"].startswith("next_data:") and r["found"] == 25 and r["count"] == 10
        assert r["rows"][0].startswith("Flat 0 | AED 100,000/yearly | bedrooms=1 size=800")
        assert r["rows"][0].endswith("https://site.example/en/plp/0.html")
        assert "more" in r
        assert mem.get("site.example")["source"].startswith("next_data")
        assert len(json.dumps(r)) < 2500  # 10 listings for well under 1k tokens


def test_xhr_feed_is_read_when_html_has_no_data(page):
    feed = {"data": {"results": [{"name": f"Job {i}", "salary": f"₹{10 + i} LPA", "company": "Acme",
                                  "url": f"/jobs/{i}"} for i in range(6)]}}
    html = """<html><body><div id=app>loading</div><script>
      fetch('/api/search?q=flutter').then(r => r.json()).then(j => app.innerText = j.data.results.length);
    </script></body></html>"""
    _serve(page, "https://jobs.example/", html, {"/api/search": ("application/json", json.dumps(feed))})
    r = listings.extract(page, "https://jobs.example/search", limit=20, query="job", wait_ms=800)
    assert r["source"].startswith("xhr:/api/search") and r["count"] == 6, r
    assert r["rows"][0] == "Job 0 | company=Acme salary=₹10 LPA | https://jobs.example/jobs/0"


def test_dom_cards_fallback(page):
    cards = "".join(f'<div class="card c{i}x"><a href="/p/{i}">Phone {i}</a><span>₹{9000 + i}</span></div>'
                    .replace(f"c{i}x", "item") for i in range(5))
    html = f'<html><body><nav><a href="/">Home</a></nav><section>{cards}</section></body></html>'
    _serve(page, "https://shop.example/", html)
    r = listings.extract(page, "https://shop.example/phones", limit=3)
    assert r["source"].startswith("dom:DIV.card.item") and r["found"] == 5 and r["count"] == 3, r
    assert r["rows"][0] == "Phone 0 ₹9000 | https://shop.example/p/0"


def test_nothing_repeated(page):
    _serve(page, "https://plain.example/", "<html><body><p>About us</p></body></html>")
    r = listings.extract(page, "https://plain.example/about")
    assert r["source"] is None and "note" in r
