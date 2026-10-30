# Tiered browser engine

Browser automation that spends Claude tokens only when it has to. Every step goes through four tiers, cheapest first, and whatever works gets cached so the next run is free.

```
cache (0 tokens) -> local LLM (free) -> Claude text (~4k) -> Claude vision (~7k)
```

Pages are read as a pruned list of interactive elements (text), not screenshots. Results are checked with DOM assertions, not "take a screenshot and ask if it worked". Screenshots are only used by the last tier.

## Measured results

**Real use (my Mac, qwen2.5:7b, Claude API tier off):** 20 runs, 24 steps. 17 succeeded, 0 Claude
tokens used, 13,943 free local-model tokens, against roughly 255,000 tokens for a screenshot-only
agent (15k per step, estimated).

**Benchmark (`bench/`).** Six read-only tasks on public sites (quotes.toscrape, books.toscrape,
Wikipedia, Hacker News, pub.dev, httpbin), scored against ground truth: the final URL, the text
actually read, and form field values. It does not trust the engine's own "ok".

| Engine version | Pass | Engine said ok | Actually correct |
|---|---|---|---|
| before the verifier fixes | cold | 4/6 | 1/6 |
| before the verifier fixes | warm (3 tasks rerun) | 3/3 | 0/3 |

What the benchmark found, and what changed:

- **False failures.** The step check trusted the model's guess about the URL, case-sensitively and even
  for typing. "Travel" didn't match `.../travel_2/`, and typing into Wikipedia's search box never
  changes the URL. Now typing, select and checkbox steps are judged by the field's actual value, and
  URL checks ignore case and separators.
- **False passes.** A click counted as done if *anything* on the page changed. On Hacker News, "click
  the new link" opened a story's site link instead, passed, and was cached, so every later run
  replayed the wrong click for free. Now, if the intent names its target and an element with exactly
  that label is on the page, a click on an unrelated element is rejected and retried with feedback.
- **Wrong region read.** Read steps sometimes picked a tag cloud or a results header. The prompt now
  steers read steps to an element inside the content; the benchmark tracks whether that is enough.

Run it yourself: `python bench/run.py` (cold pass, then warm; writes `bench/results.json`).

## Setup (macOS)

```bash
cd ~/tiered-browser-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m playwright install chromium

# Local model
brew install ollama
OLLAMA_CONTEXT_LENGTH=8192 ollama serve      # default context is too small for page trees
ollama pull qwen2.5:7b                        # 16GB RAM; try a 14B model on 32GB+

# A Chrome window the engine can drive (separate profile; log in to your sites once here)
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-engine"
```

Recent Chrome versions don't allow remote debugging on your default profile, which is why this uses
its own profile. Logins you make in that window are kept for next time.

## Connect the MCP server

**Claude Desktop / Cowork.** Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
(replace the path), then restart the app:

```json
{
  "mcpServers": {
    "browser-engine": {
      "command": "/Users/YOU/tiered-browser-engine/.venv/bin/browser-engine-mcp",
      "env": { "BE_CDP": "http://localhost:9222" }
    }
  }
}
```

**Claude Code.**

```bash
claude mcp add browser-engine --scope user -- ~/tiered-browser-engine/.venv/bin/browser-engine-mcp
```

Then install `browser-engine.skill` so Claude prefers these tools over screenshot browsing.

### Tools

| Tool | What it does |
|---|---|
| `browser_status` | Checks Ollama and Chrome are reachable, counts cached actions |
| `run_browser_plan` | Runs a list of atomic steps; returns a short failure report if a step can't be done |
| `get_page_elements` | Current page elements, ranked by a query, so the list stays short |
| `do_browser_step` | Claude does one step itself; if it works it's cached, so next time it's free |
| `browser_token_report` | Tokens used vs a screenshot-only agent, and which steps keep needing help |
| `browser_cache` | List or clear learned actions per domain |

By default the server never calls the Claude API: in Cowork or Claude Code, Claude itself is the
fallback through `do_browser_step`, so no API key is needed. Set `BE_ALLOW_CLAUDE_API=1` (plus
`ANTHROPIC_API_KEY`) in the server's `env` if you want the engine to escalate on its own.

### Safety (enforced in code)

- Typing into password, OTP and card fields, or typing card-number-like values, is always refused.
- Clicks that look irreversible (submit, send, pay, post, delete, "apply now", ...) are refused unless
  `confirm_irreversible=true`, which Claude is told to set only after you approve in chat.
- The server opens its own tab in your Chrome and never touches the tab you're using.

### Popups (0 tokens)

Before the first step on every newly loaded page, the engine closes optional popups with plain DOM
clicks: Google One Tap ("Sign in with Google") is removed, and cookie banners, "get the app" bars and
newsletter boxes are closed with their close / reject / accept / "not now" button. The button that
worked is saved per site in `~/.browser_engine/popups.json` and tried first next time. If a cached
step fails, popups are checked once more and the step is retried before any model is asked.
It never clicks anything that signs in, subscribes, buys or submits, and dialogs that your plan opened
itself (filters, date pickers) are left alone. Turn it off with `BE_DISMISS_POPUPS=0`.
Results include `popups_closed`, and `browser_cache` lists the learned buttons.

## CLI usage

The same engine also works from the terminal.

```bash
# Let Claude plan once, then run (plan is cached for next time)
browser-engine run "search for flutter developer jobs in Bengaluru" --url https://example-jobs.com

# Bring your own plan: no planner call
browser-engine run "flutter search" --url https://example-jobs.com --plan examples/plan.example.json

# Zero API spend: cache + local model only; exits with code 2 and a page report if stuck
browser-engine run "flutter search" --url https://example-jobs.com --plan plan.json --no-claude

# Use your logged-in Chrome (start Chrome with --remote-debugging-port=9222 first)
browser-engine run "..." --cdp http://localhost:9222

# Token usage vs a screenshot-only agent, plus which steps keep escalating
browser-engine report

# Inspect or reset learned actions
browser-engine cache list --domain example-jobs.com
browser-engine cache clear --domain example-jobs.com
```

Without `--cdp`, a persistent profile in `~/.browser_engine/profile` keeps your logins between runs. Add `--chrome` to use installed Google Chrome.

## Plans

A plan is a list of atomic intents. Keep concrete data in `value` so the cached action works for any value:

```json
{"steps": [
  {"intent": "type into the search box", "value": "flutter developer"},
  {"intent": "press enter to search", "value": null},
  {"intent": "click the first job result", "value": null}
]}
```

To collect data, add steps whose intent starts with "read" (for example `"read the first listing"`); the text around that listing comes back in `extracted`. Use `"go back"` steps to return to a results page. A typical loop is `open result N`, `read the product details`, `go back`: after the first product, the read and go-back steps come from the cache because product pages share a layout.

Cache entries are keyed by `(domain, normalized path, intent)`, so use the same intent wording across runs. Paths are normalized (`/jobs/48213` becomes `/jobs/:id`).

## Cost note

With the MCP server's default settings the engine never calls the Claude API. In the CLI, the Claude tiers call the Anthropic API with your API key, which is billed per token separately from a Claude Pro/Max subscription. Use `--budget 20000` to cap Claude tokens per run, or `--no-claude` to never call the API.

## Config

Environment variables: `BE_LOCAL_BASE_URL`, `BE_LOCAL_MODEL`, `BE_CLAUDE_TEXT_MODEL`, `BE_CLAUDE_VISION_MODEL`, `BE_CLAUDE_PLANNER_MODEL`, `BE_DATA_DIR`. Retry counts, element limits and the default budget are in `browser_engine/config.py`. Any OpenAI-compatible local server works (Ollama, LM Studio, `mlx_lm.server`).

## How it works

| Module | Role |
|---|---|
| `observer.py` | Extracts visible interactive elements with refs (`e12`) and stable selector candidates (id, test ids, name, aria-label, role+name, text). Passwords are masked. |
| `cache.py` | SQLite action and plan cache. An entry that fails twice in a row is dropped and re-learned. |
| `llm.py` | Local model and Claude clients. Page content is treated as untrusted data in every prompt. |
| `executor.py` | Runs actions with Playwright and verifies them with DOM checks. |
| `engine.py` | Router, escalation with feedback, learner, budget guard, JSONL step log. |
| `report.py` | Token totals vs a screenshot-loop baseline (about 15k tokens per step). |
| `safety.py` | Blocks secrets and unconfirmed irreversible actions before they run. |
| `service.py` | Stateful browser session used by the MCP tools. |
| `mcp_server.py` | MCP server (stdio); runs all browser work on one worker thread. |
| `popups.py` | Closes optional popups with DOM clicks and remembers the working button per site. |
| `guest.py` | Guest mode for remote callers: isolated browser, URL policy, login-wall stop. |
| `a2a_agent.py` | Google ADK agent + A2A server with bearer auth. |

## Share it as an A2A agent (Google ADK)

`browser-engine-a2a` serves the engine as an [A2A](https://a2a-protocol.org) agent built with Google
ADK, so a friend on your Tailscale can use it from any A2A client (ADK, LangGraph, a script). They send
one sentence, get a short answer, and spend almost no tokens: planning and page reading run here on
your local Ollama model.

It always runs in **guest mode**, so a remote caller can never act as you:

| Guest mode | Why |
|---|---|
| Own Chromium with an empty profile, thrown away after every task | Never your Chrome on :9222, never your logins or cookies |
| Login walls stop the task with `login_required` (sign-in redirect, or a password box still showing after popups are closed) | Login-only sites are not available remotely |
| Optional "sign in to save" popups are skipped ("continue as guest", ✕, "not now") | Most sites work without an account |
| localhost, 10.x / 192.168.x / 172.16.x, Tailscale 100.x, `*.local`, `*.ts.net`, `file://` blocked for every request | The agent can't be used to reach your LAN or Mac |
| Submit / pay / send / book / apply always blocked; password, OTP and card fields always refused | No one can approve them remotely |
| Captchas reported, never waited on; uploads and cache admin disabled | Nobody at your Mac is solving them |
| Bearer token on every request (the agent card is public); one task at a time | Only people you gave the token to |

### Run it (your Mac)

```bash
cd ~/tiered-browser-engine && source .venv/bin/activate
pip install -e ".[a2a]"
python -m playwright install chromium        # guest browser
OLLAMA_CONTEXT_LENGTH=8192 ollama serve       # in another terminal
browser-engine-a2a                            # binds to your Tailscale IP, port 8765
browser-engine-a2a --show-token               # send this to your friend privately
```

Options: `--host`, `--port`, or `BE_A2A_HOST`, `BE_A2A_PORT`. Planning model: `BE_A2A_MODEL`
(default `ollama_chat/qwen2.5:7b`; `gemini-2.5-flash` uses `GOOGLE_API_KEY` on the free tier and plans
more reliably). Limit sites with `BE_GUEST_ALLOW_DOMAINS=skyscanner.co.in,amazon.in` or block some with
`BE_GUEST_BLOCK_DOMAINS`. `BE_GUEST_HEADLESS=0` shows the guest window. A new token: delete
`~/.browser_engine/a2a_token` and restart.

### Use it (your friend)

```bash
export BE_A2A_URL=http://100.x.y.z:8765 BE_A2A_TOKEN=...
python examples/a2a_client.py "cheapest BLR to DEL flight on 5 Nov on skyscanner.co.in"
```

From his own ADK agent:

```python
import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

browser = RemoteA2aAgent(
    name="guru_browser",
    agent_card="http://100.x.y.z:8765/.well-known/agent-card.json",
    httpx_client=httpx.AsyncClient(headers={"Authorization": "Bearer <token>"}, timeout=600),
)
root_agent = LlmAgent(name="assistant", model="gemini-2.5-flash", sub_agents=[browser],
                      instruction="Hand any web browsing task to guru_browser.")
```

For sites that need *his* login, he runs his own copy of this repo (MCP or A2A) on his own machine.

## Limitations

The observer does not look inside iframes or shadow DOM yet, so those pages fall through to the vision tier. CAPTCHAs and bot checks are not handled and should stay manual. Small local models can misjudge complex pages, which is why every action is verified and escalates on failure. Only automate sites whose terms allow it.

## Tests

```bash
python tests/test_engine.py && python tests/test_mcp.py && python tests/test_extract.py
python tests/test_guest.py   # popups, guest mode, A2A server (needs .[a2a])
```

The live tests use a fake local model on a local HTML page, so no Ollama or API key is needed. They check that the first run learns every step and the second replays all of them from the cache, that a broken cached selector heals, that the safety rules block password fields and unconfirmed submits, that a step Claude teaches through `do_browser_step` becomes a free cache hit, and that the MCP server registers all six tools.
