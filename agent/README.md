# PKH job-fit agent

Compares a job posting against the private knowledge graph — via your own
already-deployed individual private MCP Worker — and writes a grounded fit
report. Unlike asking Claude Desktop/Code interactively (which already works,
using the same MCP server attached to this very session), this is standalone,
runnable code: a fixed system prompt, a printed tool-call trace, and code-
enforced safety rails, rather than behavior that only exists inside a chat.

There is no shared/generic private MCP server this points to by default --
each person who forks this repo has their own individual private Worker
(`<your-slug>-private.personalknowhow.com`), matching the rest of this
project's fork-and-populate design. Set `PRIVATE_MCP_URL` to yours.

## What's actually agentic here

The model is given the posting text and the private MCP server's
`query_knowhow`/`list_by_type` tools, and decides on its own:

- how to break the posting into distinct requirements
- how many times to query, and with what search terms, per requirement
- when it has enough evidence to reach a verdict

That decision-making happens **server-side**, inside Anthropic's own
infrastructure — this script never calls the MCP server's HTTP API itself.
It only opens one Messages API request (via the `mcp_servers` +
`mcp_toolset` connector) and, at the end, writes whatever text came back to
a file. No local tool-execution loop, no local write-tool the model can
invoke — see Safety notes below for why.

## Setup

```
pip install -r ../requirements.txt   # adds `anthropic`

export ANTHROPIC_API_KEY=...         # your own key -- billed per request, not currently set anywhere in this environment
export PRIVATE_MCP_URL=...           # your own individual private Worker, e.g. https://<your-slug>-private.personalknowhow.com/mcp -- no shared default
export PRIVATE_MCP_TOKEN=...         # the bearer token for that same Worker (set via `wrangler secret put PRIVATE_MCP_TOKEN` when it was deployed -- the script adds "Bearer " itself)
```

## Run

```
python job_fit_agent.py path/to/posting.txt
python job_fit_agent.py -                                # read posting from stdin
python job_fit_agent.py https://example.com/careers/role  # fetch a posting URL
```

**Not LinkedIn URLs.** The script refuses to auto-fetch anything on
`linkedin.com` — the full posting is behind a login wall anyway, and this
project never makes automated HTTP requests against LinkedIn on a real
account (job data here is always collected interactively, via Playwright MCP
with human pacing — see `JOB_MARKET_ANALYSIS.md`). For a LinkedIn posting,
paste the description text into a file instead.

For any other URL, fetching is tiered:

1. **Direct GET** — fast, no third party involved, strips the HTML down to
   visible text (dropping `<script>`/`<style>` content, preserving paragraph
   and list breaks). Works fine on ordinary server-rendered pages.
2. **Reader-proxy fallback** (`r.jina.ai`) — kicks in automatically when the
   direct fetch comes back empty or too short, which happens on JS-rendered
   pages and on sites with bot-throttling (confirmed live against a
   Cloudflare-fronted Next.js careers page that defeated both a plain GET
   and Claude's own web-fetch tool — the reader proxy recovered the full
   posting). Still just an HTTP GET on this end, no local browser binaries —
   deliberately *not* a local Playwright/headless-browser fallback, which
   would only work on whatever machine has it installed and would never work
   from a phone. The target URL (not corpus data) passes through this third
   party as part of this fallback.

If both tiers come back too short, the script tells you to paste the text
instead rather than silently running on empty content.

## What it does

1. Sends the posting to Claude Opus 5 with the private MCP server attached.
2. The model decomposes the posting into requirements and queries each one
   separately — printed to stderr as it happens, so the chaining is visible,
   not just the final answer.
3. Respects `evidence_tier`: a `signal_only` result (job applications, saved
   jobs, career interests, saved answers) is never counted as proof of
   ability, only noted separately if relevant — same rule the private
   Worker's own tool descriptions already state.
4. Prints the Markdown report and saves it to `../data/job_fit_reports/`
   (never overwrites — a second run on the same posting the same day gets a
   time-suffixed filename).

## Safety notes

- Read-only end to end. No tool anywhere in this script can write, post, or
  apply to anything — the only file it creates is the report, after the
  model's turn is already finished, in plain Python, not via a tool the
  model calls.
- Run manually, like every other script in this repo — not wired into any
  cron or CI, consistent with PKH's no-automation-on-personal-data policy.

---

# token_usage_benchmark.py

Measures real, measured token/cost/latency numbers comparing two ways of
answering questions about a personal knowledge corpus: attaching raw
LinkedIn export CSVs as Files API documents ("Method A", a disclosed proxy
for uploading files to a Claude Project) vs. querying the public
`personalknowhow-demo` MCP Worker ("Method B", same connector pattern as
`job_fit_agent.py` above). Built as a follow-up to a published article
making the same comparison qualitatively, with no measured numbers — see
`~/.claude/plans/that-s-going-to-be-valiant-bachman.md` for the full design.

**This is the first script in this repo to read `response.usage` at all.**

## Setup

```
pip install -r ../requirements.txt   # adds `anthropic`
export ANTHROPIC_API_KEY=...         # your own key -- billed per request
```

No `PRIVATE_MCP_URL`/`PRIVATE_MCP_TOKEN` needed for the default run — Method
B points at the public, no-auth demo Worker. Those two vars are only needed
for the optional `--private-evidence-extension` (see below).

## Run

**Always `--dry-run` first** — zero network calls, a local token estimate,
and the only place a context-window overflow gets caught before it costs
anything:

```
python token_usage_benchmark.py --export-dir ~/Documents/PKH_raw_export_archive_2026-08-10/Complete_LinkedInDataExport_07-07-2026.zip --dry-run
```

Then a small, bounded smoke test before trusting the numbers for anything
public:

```
python token_usage_benchmark.py --export-dir <same path> --questions q3,q5 --methods both
```

Then the full run, once the smoke test's `usage` numbers and answers have
been manually checked for plausibility:

```
python token_usage_benchmark.py --export-dir <same path>
```

Useful flags: `--methods files|graph|both`, `--questions q1,q4,q7` (cost
bounding), `--model claude-sonnet-5` (cheaper trial runs), `--yes` (skip the
interactive spend confirmation, for scripted use).

## Method A's file scope

`--export-dir` should point at the real raw LinkedIn export (the unpacked
`Complete_LinkedInDataExport_*` directory, archived outside this repo at
`~/Documents/PKH_raw_export_archive_2026-08-10/` per the root `CLAUDE.md`).
By default, only the files listed in `token_usage_benchmark_files.txt` are
attached — a safer subset that excludes `messages.csv`, `PhoneNumbers.csv`,
`Connections.csv`, and other third-party-PII or signal-only files, so
nothing sensitive gets sent through the API just to measure token counts.
`--all-files` opts into the full export instead (explicit, never a silent
default) — note that the full export is large enough it may approach or
exceed the model's context window on its own; `--dry-run` will warn if so.

## Output

`../data/token_usage_benchmark/<date>.json` (full per-question detail plus a
`methodology` block — model, pricing source/date, exact file manifest, MCP
URL, and the Files-API-proxy disclosure) and a human-readable `<date>.md`
summary table, including an efficiency-ratio row (Method A tokens ÷ Method B
tokens). Never overwrites — a same-day re-run gets a time-suffixed filename.
`data/` is gitignored; publishing specific numbers into an article remains a
manual, human step.

## Safety notes

- Costs real money past `--dry-run` — always dry-run first, then a bounded
  `--questions` smoke test, before a full run.
- `--private-evidence-extension` is off by default and, if used, writes to a
  separately-named `..._private_evidence.json` file — never merged into the
  public-tier headline numbers, since only the private tier has a
  `demonstrated`/`signal_only` field to test against.
- Run manually, same no-cron/no-CI policy as every other script here.
