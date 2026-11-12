"""Offline replicas of bench/tasks.json pages (no network, no model): counts steps finished with
local=None, i.e. by cache + label matching alone.  Run: python bench/offline.py"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from browser_engine.cache import ActionCache
from browser_engine.config import Config
from browser_engine.engine import Engine, StepFailed, StepLog
P = {
 "quotes.html": '<h1>Quotes to Scrape</h1><a href="login.html">Login</a>' + "".join(f'<div class=quote><span>“Quote {i}”</span> by <small>Author {i}</small> <a href="about{i}.html">(about)</a> Tags: <a href="tag{i}.html">life</a></div>' for i in range(10)) + '<li class=next><a href="page2.html">Next <span>→</span></a></li>',
 "books.html": '<h1>All products</h1><ul>' + "".join(f'<li><a href="cat_{c.lower()}.html">{c}</a></li>' for c in ["Books","Travel","Mystery","Historical Fiction","Sequential Art","Classics","Philosophy","Romance"]) + '</ul>' + "".join(f'<article><h3><a href="b{i}.html">Book {i}</a></h3><p>£{10+i}.00</p><button>Add to basket</button></article>' for i in range(20)),
 "wiki.html": '<a href="main.html">Main page</a><a href="contents.html">Contents</a><form action="article.html"><input type=search name=search placeholder="Search Wikipedia" aria-label="Search Wikipedia"><button>Search</button></form><a href="random.html">Random article</a>',
 "hn.html": '<a href="hn.html">Hacker News</a> <a href="newest.html">new</a> | <a href="past.html">past</a> | <a href="comments.html">comments</a>' + "".join(f'<p>{i}. <a href="s{i}.html">New startup story {i}</a> (<a href="from{i}.html">example{i}.com</a>)</p>' for i in range(30)),
 "pub.html": '<a href="flutter.html">Flutter</a><a href="dart.html">Dart</a><form action="packages.html"><input name=q placeholder="Search packages" aria-label="Search packages"></form>',
 "form.html": '<form><p><label>Customer name: <input name=custname></label></p><p><label>Telephone: <input type=tel name=custtel></label></p><p><label>E-mail address: <input type=email name=custemail></label></p><fieldset><legend>Pizza Size</legend><label><input type=radio name=size value=small> Small</label><label><input type=radio name=size value=medium> Medium</label><label><input type=radio name=size value=large> Large</label></fieldset><button>Submit order</button></form>',
}
TASKS = [("quotes.html", [("click the Next page link", None)]),
         ("books.html", [("click the Travel category in the sidebar", None)]),
         ("wiki.html", [("type into the search box", "Flutter (software)"), ("press enter to search", None)]),
         ("hn.html", [("click the new link in the top bar", None)]),
         ("pub.html", [("type into the package search box", "flutter_on_device_rag"), ("press enter to search", None)]),
         ("form.html", [("type into the customer name field", "Guru Test"), ("type into the telephone field", "5550100"), ("click the Medium pizza size option", None)])]
d = tempfile.mkdtemp()
for n, h in P.items(): open(os.path.join(d, n), "w").write(f"<!doctype html><html><body>{h}</body></html>")
for n in ["page2.html", "cat_travel.html", "article.html", "newest.html", "packages.html"]:
    open(os.path.join(d, n), "w").write("<html><body><h1>ok</h1></body></html>")
from playwright.sync_api import sync_playwright
done = total = 0
with sync_playwright() as pw:
    b = pw.chromium.launch(); page = b.new_page()
    cfg = Config(); cfg.data_dir, cfg.settle_ms = d, 50
    cache = ActionCache(cfg.cache_path)
    for f, steps in TASKS:
        page.goto("file://" + os.path.join(d, f))
        eng = Engine(page, cfg, cache, StepLog(cfg.log_path), local=None, claude=None)
        for intent, val in steps:
            total += 1
            try:
                eng.do_step(intent, val); done += 1
            except StepFailed:
                break
print(json.dumps({"steps": total, "done_without_any_model": done}))
