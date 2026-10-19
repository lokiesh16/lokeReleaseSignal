# API Test Generator

Captures live network traffic from a real browser session, lets you pick which
API calls make up a test scenario, detects how values chain between them
(e.g. a login token or a created-order id flowing into a later request), and
asks an LLM to generate a `requests` + `pytest` test suite for that chain.

This is a smaller, API-only sibling of `testgenforWeb_v2` — same network
capture engine and API-chain detector/generator, with the DOM capture, UI
Playwright script generation, codebase analyzer, and Chrome extension
stripped out.

## Setup

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Install Playwright's browser
playwright install chromium

# 3. Start the server
source venv/bin/activate
python server.py

# 4. Open the UI
open http://localhost:5002
```

## Usage

1. **Capture** — enter the target app's URL and click **Launch browser**. A
   real Chromium window opens; log in and use the app normally. Every
   fetch/XHR call it makes (method, headers, body, response) shows up live in
   the Network requests panel.
2. **Select** — check the boxes on the calls that make up your scenario
   (e.g. login → create order → fetch order). They're pinned in the "API
   chain test" box below and survive clearing the live log.
3. **Analyze chain** — runs a deterministic (non-AI) scan that looks for a
   value in one response (a token, an id) reappearing in a later request's
   header, body, or URL, so the generator knows exactly what to wire together.
4. **Generate** — pick a provider (Claude, or a local Ollama model), describe
   the scenario in a sentence, and generate. Output is an API-client module
   plus a pytest file that runs the chain once in `setup_class` and asserts
   on the real response data in small, focused test methods.
5. **Preview & download** — syntax-highlighted preview, copy or download
   individual files, or "Download all" to save into `generated_scripts/`.

## Record a UI test

Besides API chains, you can record UI automation by hand: **Record UI test**
in the sidebar.

1. Launch the browser on the Capture step, then click **Start recording**.
2. Use the app normally. Clicks, typing, dropdowns, checkboxes and
   Enter/Escape show up as steps (editable, removable). A toolbar in the page
   adds the rest: **🎯 Pick** highlights an element *without clicking it* and
   offers a menu — click, hover, scroll into view, wait for element, assert
   visible / text / value / href — plus a timed **Wait** and scroll top/bottom.
   Esc leaves pick mode. **⚓ Anchor** locates controls by their nearest label
   (e.g. the button under “Date of Birth”) rather than their current text, for
   values that change; every step also keeps such an alternative you can swap in with ✎. **▶ Replay steps** checks them in a fresh window.
3. Name the scenario and **Generate test** → a self-contained
   pytest + Playwright file at `generated_scripts/ui_tests/test_<name>.py`.
   Locators prefer test-id > role+name > label > placeholder > text > css.
   Generation is deterministic (no LLM).
4. **Run test** replays it in a fresh browser (headed, with optional slow-mo)
   and streams the pytest output.

**Saved recordings:** name and save the steps (💾), reload one later to edit it
(change values, fix selectors, add waits, split cases, delete steps, or **Record
more (append)**), and save again or delete it. Steps are also autosaved, so a
restart doesn't lose them. Files live in `ui_recordings/` (owner-only, gitignored)
and contain the recorded values, passwords included.

Password-like fields are never written into the script: they're read from an
env var (e.g. `UI_PASSWORD`), with the recorded value kept in an owner-only
`.<name>.env` next to the test.

Limits: top-level frame only (no iframes), no file uploads / drag-and-drop /
hover, new tabs and popups aren't followed. Regenerating overwrites the file.

## Framework config

`prompt_config.json` controls the conventions the LLM is told to follow:
client-class architecture, auth header/session pattern, chaining rules, and
the exact `config_manager` methods it's allowed to call. Edit it directly, or
through the **⚙ Framework config** button in the UI, to match your own
project instead of the generic placeholder shipped here.

## Project structure

```
api-testgen/
├── server.py               ← Flask + Socket.IO backend
├── api_chain_analyzer.py   ← deterministic response→request value-chain detector
├── network_inject.js       ← in-page fetch/XHR/beacon interceptor (fallback capture path)
├── ui_recorder.js          ← in-page UI action recorder (clicks/fills/selects → steps)
├── ui_test_generator.py    ← recorded UI steps → pytest + Playwright file
├── index.html              ← web UI
├── prompt_config.json      ← framework/client-architecture rules injected into prompts
└── generated_scripts/      ← "Download all" saves here
```





<!-- Looked through server.py, api_chain_analyzer.py, and the prompt_config-driven generation flow to ground these in what's actually there today (not generic ideas). Grouped by area:

Capture

Persist sessions — network log lives in-memory per Playwright page; a browser crash or Stop all loses everything uncaptured. Save to disk/SQLite so a session survives a restart.
Save/reload named sessions — let a user capture once, save the whole network log as a named scenario, and reopen it later instead of re-driving the app every time.
WebSocket/SSE capture — is_api_url() and the Playwright listeners only see fetch/XHR; anything over WS is invisible today.
Auto-redact secrets before they hit the log/prompt/LLM — Authorization, cookies, and API keys currently flow through raw into requestHeaders and then into the Claude/Ollama prompt.
Chain detection (api_chain_analyzer.py)

It's substring/equality matching only — no JWT-payload decoding (a token often encodes the id you're trying to match, not just carry it literally), no cookie-based session chains, no GraphQL variable/response mapping.
Multi-hop chains — it links step→step but doesn't visualize a value threading through 3+ steps, which would help review longer scenarios.
Confidence scoring — right now every match is shown equally; a numeric/short value match is far less trustworthy than a UUID/token match.
Generation

Contract/schema validation tests — generate JSON-schema assertions from the captured response shape, not just value-equality checks.
Multiple scenarios per run — currently one scenario → one client+test pair; batching several selected chains into one generation call (or a suite) isn't supported.
Negative-path tests — only the happy path captured live gets tested; an option to ask the LLM for auth-failure/400/404 variants of the same chain would extend coverage for free.
Diff-aware regeneration — re-running generation for an updated chain overwrites everything; preserving hand-edits in previously generated files isn't supported.
Execution / feedback loop

Run the generated pytest file from the UI and show pass/fail inline — right now "Download all" is the end of the pipeline; there's no way to know if the generated test actually passes without leaving the app.
CI export — a button to scaffold a GitHub Actions workflow around generated_scripts/.
Interop

Import/export OpenAPI, Postman, or HAR — capture currently only comes from live Playwright browsing; importing an existing Postman collection or HAR file would let people test APIs without driving a UI at all.
Export captured calls as curl — handy for quick manual debugging outside the generated test.
Config/collaboration

prompt_config.json is a single global file — supporting per-project config profiles (switchable in the UI) would let one instance serve multiple codebases' conventions.
Multi-provider generation diffing — since both Claude and Ollama are already wired up, a "generate with both, compare" mode is nearly free to add.
If any of these are interesting, I'd start with persisting sessions and running the generated test from the UI — they're the two gaps that most directly block someone from actually trusting the output today. Want me to scope one of these out? -->