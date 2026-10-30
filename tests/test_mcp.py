"""Service + MCP server tests. Run: python tests/test_mcp.py"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_engine.config import Config  # noqa: E402
from browser_engine.llm import Usage  # noqa: E402
from browser_engine.safety import Blocked, Guard  # noqa: E402
from browser_engine.service import BrowserService  # noqa: E402

FORM = """<!doctype html><html><body>
<h1>Apply</h1>
<input aria-label="Full name" name="fullname">
<input type="password" aria-label="Password" name="pw">
<button id="later" onclick="document.title='saved'">Save for later</button>
<button id="submit" onclick="document.title='submitted'">Submit application</button>
</body></html>"""


class NameOnlyLocal:
    """Knows how to fill the name and click buttons; gives up on anything else."""

    def decide(self, intent, value, obs, feedback=None):
        i = intent.lower()
        pick = None
        if "name" in i:
            pick = ("type", next(e.ref for e in obs.elements if e.name == "Full name"))
        elif "password" in i:
            pick = ("type", next(e.ref for e in obs.elements if e.name == "Password"))
        elif "submit" in i:
            pick = ("click", next(e.ref for e in obs.elements if e.name.startswith("Submit")))
        if not pick:
            return json.dumps({"action": "fail", "reason": "no idea"}), Usage(100, 10)
        return json.dumps({"action": pick[0], "ref": pick[1]}), Usage(100, 10)


def test_guard_rules():
    g = Guard()
    for args in [("type the password", "type", "x", {}),
                 ("type into name", "type", "x", {"type": "password"}),
                 ("type into box", "type", "4111 1111 1111 1111", {}),
                 ("click the submit button", "click", None, {}),
                 ("click it", "click", None, {"label": "Pay now"})]:
        try:
            g.check(*args)
            raise AssertionError(f"not blocked: {args}")
        except Blocked:
            pass
    g.check("type into the job search box", "type", "flutter", {"label": "Search jobs"})
    g.check("click apply filters", "click", None, {"label": "Apply filters"})
    Guard(confirm_irreversible=True).check("click the submit button", "click", None, {})


def test_service_flow():
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as d:
        html = os.path.join(d, "form.html")
        with open(html, "w") as fh:
            fh.write(FORM)
        url = "file://" + html
        cfg = Config()
        cfg.data_dir, cfg.settle_ms = d, 100
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            svc = BrowserService(cfg, local=NameOnlyLocal(), claude=None,
                                 page_factory=lambda: browser.new_page())

            # 1. Irreversible step is blocked without confirmation; earlier steps still ran.
            r = svc.run_plan("apply", [{"intent": "type into the name field", "value": "Guru"},
                                       {"intent": "click the submit button"}], start_url=url)
            assert not r["ok"] and r["blocked"] and r["completed"] == 1, r
            assert svc.page.title() != "submitted"

            # 2. Password fields are always refused, even with confirmation.
            r = svc.run_plan("pw", [{"intent": "type the password", "value": "hunter2"}],
                             start_url=url, confirm_irreversible=True)
            assert not r["ok"] and r["blocked"], r

            # 3. Local model gives up -> compact failure report -> Claude teaches one step.
            r = svc.run_plan("save", [{"intent": "click save for later"}], start_url=url)
            assert not r["ok"] and not r["blocked"]
            assert any("Save for later" in line for line in r["page"]["elements"]), r["page"]
            assert len(json.dumps(r)) < 2000  # stays small for Claude's context
            els = svc.page_elements("save for later", limit=5)
            ref = next(line.split()[0] for line in els["elements"] if "Save for later" in line)
            t = svc.do_step("click save for later", "click", ref=ref)
            assert t["ok"] and t["learned"], t

            # 4. Next run: the taught step is a free cache hit.
            r = svc.run_plan("save", [{"intent": "click save for later"}], start_url=url)
            assert r["ok"] and r["tiers"] == {"cache": 1}, r

            # 5. Confirmed submit goes through.
            r = svc.run_plan("apply", [{"intent": "click the submit button"}], start_url=url,
                             confirm_irreversible=True)
            assert r["ok"] and svc.page.title() == "submitted", r

            # 6. Tab closed behind our back: next call recovers on its own.
            svc.page.close()
            r = svc.run_plan("save", [{"intent": "click save for later"}], start_url=url)
            assert r["ok"] and r["tiers"] == {"cache": 1}, r

            rep = svc.report()
            assert rep["claude_tokens"] == 0
            print("\nreport:", rep["by_tier"], "| cache:", svc.cache_op()["count"], "entries")
            browser.close()


def test_mcp_server_registration():
    from browser_engine import mcp_server

    async def go():
        tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
        assert set(tools) == {"browser_status", "run_browser_plan", "get_page_elements",
                              "do_browser_step", "browser_token_report", "browser_cache", "wait_for_human"}
        schema = tools["run_browser_plan"].inputSchema
        assert "steps" in schema["required"]
        os.environ["BE_DATA_DIR"] = tempfile.mkdtemp()
        out = await mcp_server.mcp.call_tool("browser_status", {})
        return out

    out = asyncio.run(go())
    print("status tool output:", str(out)[:160])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
