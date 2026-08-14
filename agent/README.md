# PKH job-fit agent

Compares a job posting against the private knowledge graph — via the already
deployed `personalknowhow-private` MCP server — and writes a grounded fit
report. Unlike asking Claude Desktop/Code interactively (which already works,
using the same MCP server attached to this very session), this is standalone,
runnable code: a fixed system prompt, a printed tool-call trace, and code-
enforced safety rails, rather than behavior that only exists inside a chat.

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
export PRIVATE_MCP_TOKEN=...         # the raw token in ../mcp-private/URLs.txt (the script adds "Bearer " itself)
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
