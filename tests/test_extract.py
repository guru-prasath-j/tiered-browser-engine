"""Extract + back on a multi-city stays search and a product-detail loop. Run: python tests/test_extract.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.config import Config  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.service import BrowserService  # noqa: E402

STAYS = """<!doctype html><html><body>
<form action="stays.html"><input name="city" aria-label="Where to" placeholder="Where to"></form>
<main id="r"></main>
<script>
const c = new URLSearchParams(location.search).get('city');
if (c) document.getElementById('r').innerHTML = [1,2,3].map(i =>
  `<div class="card"><a href="#s${i}">${c} stay number ${i}</a>
   <div>₹${2000+i*700} night</div><div>4.${9-i} ★ (12${i} reviews)</div></div>`).join('');
</script></body></html>"""

SHOP = """<!doctype html><html><body><h1>Results</h1>
<a href="item.html?n=1">Earbuds One</a><a href="item.html?n=2">Earbuds Two</a></body></html>"""
ITEM = """<!doctype html><html><body><div id="d">
<h1 id="productTitle"></h1><div>₹1,799</div><div>4.2 out of 5 stars, 3,410 ratings</div></div>
<button>Add to cart</button>
<script>document.getElementById('productTitle').textContent =
  'Earbuds model ' + new URLSearchParams(location.search).get('n');</script></body></html>"""


class Local:
    def decide(self, intent, value, obs, feedback=None):
        i = intent.lower()
        els = obs.elements
        links = [e for e in els if e.role == "link"]
        if "where to" in i or "destination" in i:
            d = {"action": "type", "ref": next(e.ref for e in els if e.role == "textbox")}
        elif "press enter" in i:
            d = {"action": "press", "text": "Enter", "ref": next(e.ref for e in els if e.role == "textbox")}
        elif "read" in i and "listing" in i:
            k = 0 if "first" in i else 1
            d = {"action": "extract", "ref": links[k].ref}
        elif "open result" in i:
            k = int(i.split()[-1]) - 1
            d = {"action": "click", "ref": links[k].ref}
        elif "read the product" in i:
            d = {"action": "extract", "ref": next(e.ref for e in els if e.role == "heading")}
        elif "go back" in i:
            d = {"action": "back"}
        else:
            d = {"action": "fail", "reason": "?"}
        return json.dumps(d), Usage(200, 20)


def test_multi_city_and_product_loop():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d:
        for name, html in (("stays.html", STAYS), ("shop.html", SHOP), ("item.html", ITEM)):
            open(os.path.join(d, name), "w").write(html)
        cfg = Config()
        cfg.data_dir, cfg.settle_ms = d, 100
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            svc = BrowserService(cfg, local=Local(), claude=None, page_factory=lambda: browser.new_page())

            city_steps = lambda city: [
                {"intent": "type into the where to box", "value": city},
                {"intent": "press enter to search"},
                {"intent": "read the first listing"},
                {"intent": "read the second listing"},
            ]
            tiers = []
            for city in ("Goa", "Jaipur", "Manali"):
                r = svc.run_plan("stays", city_steps(city), start_url="file://" + d + "/stays.html")
                assert r["ok"], r
                texts = [x["text"] for x in r["extracted"]]
                assert len(texts) == 2 and all("₹" in t and city in t for t in texts), texts
                tiers.append(r["tiers"])
            print("\nstays tiers per city:", tiers)
            print("sample extract:", r["extracted"][0]["text"].replace("\n", " | "))
            assert tiers[1].get("cache", 0) >= 2 and tiers[2].get("cache", 0) >= 2  # search steps replayed

            # Product loop: open, read, back, twice. The read + back of item 2 should be cache hits.
            steps = []
            for n in (1, 2):
                steps += [{"intent": f"open result {n}"}, {"intent": "read the product details"}, {"intent": "go back"}]
            r = svc.run_plan("shop", steps, start_url="file://" + d + "/shop.html")
            assert r["ok"], r
            assert [x["text"].splitlines()[0] for x in r["extracted"]] == ["Earbuds model 1", "Earbuds model 2"]
            assert "out of 5 stars" in r["extracted"][1]["text"]
            print("product loop tiers:", r["tiers"])
            assert r["tiers"].get("cache", 0) >= 2
            browser.close()


if __name__ == "__main__":
    test_multi_city_and_product_loop()
    print("ok test_multi_city_and_product_loop")
