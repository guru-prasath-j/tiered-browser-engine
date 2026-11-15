# Tiered browser engine

Browser automation that spends Claude tokens only when it has to. Every step goes through four tiers, cheapest first, and whatever works gets cached so the next run is free.

```
cache (0) -> label match (0) -> local LLM (free) -> Claude text (~4k) -> Claude vision (~7k)
```

## v0.7: what changed, measured

Built from the real step log (26 runs, 81 steps): 40% of steps had to be done by Claude by hand,
and about half of the local model's failures were false: the action worked, but the model's own
guess about the next URL or page text (e.g. a date written as `261029`) didn't match.

| | v0.6 | v0.7 |
|---|---|---|
| Action steps of the 6 bench tasks done with **no model at all** (offline replicas, cold cache) | 0 / 10 | 10 / 10 |
| Page text in a local-model prompt (150-element page) | ~760 tokens | ~190 tokens |
| Failure report Claude reads | ~330 tokens | ~190 tokens |
| Fixed per-turn cost of tool definitions + instructions | ~2,050 tokens | ~1,660 tokens (with one tool more) |

- **Label match tier (0 tokens).** If the step names a label that exactly one element of a fitting
  role carries ("type into the telephone field", "click the Next page link"), it is done without a
  model, then verified and cached like any other step. Ambiguous or positional steps ("the first
  result") still go to the model.
- **Effects, not guesses.** A step is judged by what happened (field value, checkbox state,
  navigation, page change). The model's guessed URL/text never fails a working step, and is only
  cached if it actually held. The local model no longer writes guesses at all.
- **Fixes for the logged failures:** formatted phone fields (`+91 80567 08293`), several refs in one
  reply, custom (non-`<select>`) dropdowns, clicks on covered elements (scroll + retry + DOM click),
  cached selectors that match a hidden duplicate.
- **Smaller prompts.** A model sees the 30 elements most related to the step (80 on retry).
- **Claude side:** `do_browser_step(next_intent=...)` returns the next step's elements, so no
  extra `get_page_elements` call; empty fields are dropped from results; failure reports carry 12
  elements; `run_browser_plan(task_name=...)` saves a working plan, and later
  `run_browser_plan(task_name, values)` replays it with no steps written at all.
- **`extract_listings(url)`**: reads listings/products/jobs as one compact line per item from the
  page's embedded data, its own JSON feed, or repeated DOM cards. No model. The source that worked
  is remembered per site in `~/.browser_engine/sites.json`.
- `browser_token_report`, `browser_cache` and `browser_usage_log` are now one tool, `browser_info`.

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
| `extract_listings` | Listings/products/jobs on a page as compact rows from the site's own data (0 tokens) |
| `browser_info` | `report` (tokens vs screenshot agent), `usage` (calls.jsonl log), `cache_list` / `cache_clear`, `tasks` |

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
Results include `popups_closed`, and `browser_info` with `op: cache_list` lists the learned buttons.

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

## Visual logs: see exactly where a run failed (v0.9)

```bash
browser-engine logs            # opens http://127.0.0.1:8772  (Browser Helper: the "Logs" link, /logs)
browser-engine logs --prune    # just delete logs older than 24h
```

Every run is shown with its goal and steps in order. Each step shows the chain of tiers it went
through (rule → cache → label match → local LLM → Claude …), with the reason every failed attempt
gave. The step that broke the run is marked red with the page URL and a **screenshot taken at the
moment it failed** (bot checks and blocked/irreversible steps too). Filters: Failed, Retried
(succeeded only after a failed attempt), Needs you (bot check). It also shows "Where failures happen
most", tier mix and token use. The page refreshes every 10 s.

**24-hour retention:** `steps.jsonl`, `calls.jsonl` and failure screenshots (`~/.browser_engine/shots/`)
older than 24 hours are deleted when the engine starts, every 10 minutes while it runs, and whenever the
log page loads. Each run shows its own countdown ("deleted in 3.2h"). Env: `BE_LOG_RETENTION_HOURS`
(default 24), `BE_FAIL_SHOTS=0` to turn screenshots off. Log writes use a file lock, so pruning never
drops lines another process (MCP server, Browser Helper) is writing. The page listens on 127.0.0.1 only.

## Use it yourself, or give it to a non-technical person: Browser Helper (v0.8)

`browser-engine-home` is a personal agent (Google ADK) with a local chat page at http://127.0.0.1:8770.
It drives a browser window on **your own** computer with a saved profile, so a site you sign in to once
stays signed in. You type passwords, OTPs and payments yourself (safety.py refuses those fields).
Submit, book and send need a "yes" in the chat for that exact action (`user_confirmed`). The planner
is Gemini (free key) or local Ollama. The page binds to 127.0.0.1 only and checks Host/Origin, so other
sites and other devices can't drive it.

```bash
bash install.sh                 # macOS/Linux one-step setup (Windows: install.ps1), adds a Desktop launcher
browser-engine-home             # or double-click "Browser Helper" on the Desktop
browser-engine-home --set-key   # change the Gemini key (stored in ~/.browser_engine/home.env, mode 600)
```

Plain-language guide for someone else: [HOME_SETUP.md](HOME_SETUP.md). Env: `BE_HOME_MODEL` (any
LiteLLM id), `BE_HOME_PORT`.

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
python tests/test_logviz.py  # visual logs: 24h pruning, run grouping, viewer server
python tests/test_home.py    # home agent chat page, settings, model choice (needs .[home])
python tests/test_guest.py   # popups, guest mode, A2A server (needs .[a2a])
```

The live tests use a fake local model on a local HTML page, so no Ollama or API key is needed. They check that the first run learns every step and the second replays all of them from the cache, that a broken cached selector heals, that the safety rules block password fields and unconfirmed submits, that a step Claude teaches through `do_browser_step` becomes a free cache hit, and that the MCP server registers all six tools.

## v0.5.0: custom form questions and pre-submit check

Many application forms draw radio buttons as styled labels over hidden inputs, or as plain
clickable divs, so they never show up in `get_page_elements`. Two new MCP tools handle them with
no model at all (0 tokens):

- `choose_option(question, option)` picks the answer by visible text, e.g.
  `choose_option("years of experience as a Backend Engineer with Python", "1 - 2 years")`.
  Works for native radios/checkboxes/selects, hidden "sr-only" inputs, ARIA radios/options and
  clickable divs. Question matching is fuzzy (word overlap), option matching prefers exact text.
  In a plan, use the step `{"intent": "answer: <question>", "value": "<option>"}`.
- `check_form()` lists required questions that are still unanswered (labels ending in `*`,
  `required` / `aria-required` fields, empty radio groups). Call it before any Send/Submit step.

The observer now also lists hidden radios/checkboxes whose label is visible, and `check` /
`uncheck` fall back to clicking the label or the element when Playwright can't tick a hidden input.
Tests: `tests/test_forms.py`.

## v0.6.0: query and token usage log

Every MCP tool call is appended to `~/.browser_engine/calls.jsonl`: the tool, what was asked
(goal, start URL, step intents, query, ref), whether it worked, how long it took, the estimated
Claude tokens for the request and the result (chars / 4), and the local-model / Claude API tokens
the engine spent during that call.

```bash
browser-engine usage                 # totals, per-tool table, heaviest results, recent calls
browser-engine usage --hours 24      # last day only
browser-engine usage --tool get_page_elements --last 50
browser-engine usage --json
browser-engine calls --last 10       # raw JSON lines
tail -f ~/.browser_engine/calls.jsonl
```

From Claude, ask for `browser_info` with `op: usage` (same summary). Set `BE_LOG_CALLS=0` to turn the
log off, or `BE_LOG_VALUES=0` to keep typed values (names, emails, answers) out of it.
