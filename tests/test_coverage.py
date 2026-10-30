"""Partial-list guard tests. Run: python tests/test_coverage.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.observer import Element, Observation  # noqa: E402
from browser_engine.service import compact_page  # noqa: E402


def _obs(n, more_button=False):
    els = [Element(ref=f"e{i}", role="link", name=f"Flight option {i}: Total cost Rs {15000 - i * 100}",
                   tag="a", in_view=i < 3) for i in range(n)]
    if more_button:
        els.append(Element(ref="e99", role="button", name="Show more results", tag="button"))
    return Observation(url="https://x.test/r", title="r", headings=[], elements=els)


def test_warns_when_matches_hidden():
    page = compact_page(_obs(12), "flight option total cost", limit=4)
    assert page["matching"] == 12 and page["matching_not_shown"] == 8, page
    assert "warning" in page


def test_warns_on_load_more():
    page = compact_page(_obs(3, more_button=True), "flight option total cost", limit=30)
    assert "matching_not_shown" not in page
    assert page["load_more"] and "warning" in page


def test_quiet_when_everything_seen():
    page = compact_page(_obs(5), "flight option total cost", limit=30)
    assert page["matching"] == 5 and "warning" not in page, page


def test_warns_when_page_list_capped():
    page = compact_page(_obs(150), "flight", limit=150, max_elements=150)
    assert page.get("page_list_capped") and "warning" in page


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
