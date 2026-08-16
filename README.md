[![PersonalKnowHow](design/social/github-social-preview.png)](https://personalknowhow.com)

**[Join the Waitlist](https://personalknowhow.com)** · [Live Demo](#try-the-live-demo) · [Issues](https://github.com/Georgi-Petkov/personalknowhow/issues)

Turns a scattered personal learning/work history — LinkedIn, GitHub, course platforms, Gmail
completion emails, sibling project repos — into a unified knowledge graph, served over two
real, deployed [MCP](https://modelcontextprotocol.io/) servers so any MCP-aware client (Claude
Desktop, etc.) can query it with semantic search instead of keyword matching.

## Try the live demo

`https://personalknowhow-demo.kxtwrdzt6g.workers.dev/mcp` is a real, deployed MCP server — but
**it's not a webpage.** Opening that URL in a browser sends a plain `GET`, and MCP servers only
speak `POST` with JSON-RPC framing, so you'll just see a bare `{"error":{"message":"Method not
allowed."}}`. That's expected, not broken — it means you're looking at it the wrong way.

The actual way to use it is as an MCP connector. In Claude Desktop, edit
`claude_desktop_config.json` ([config file location](https://modelcontextprotocol.io/quickstart/user)):

```json
{
  "mcpServers": {
    "personalknowhow-demo": {
      "command": "npx",
      "args": ["mcp-remote", "https://personalknowhow-demo.kxtwrdzt6g.workers.dev/mcp"]
    }
  }
}
```

Restart Claude Desktop, then ask something like *"use personalknowhow-demo to check if I have
Django experience"* — Claude calls the `query_knowhow` tool over MCP and gets back
semantically-matched evidence (courses, projects, certifications) with similarity scores, no
auth required.

If you just want to confirm the server is alive without setting up a client:

```bash
curl -s https://personalknowhow-demo.kxtwrdzt6g.workers.dev/mcp \
  -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

A `200` with a JSON-RPC response back confirms it's live — the `Accept` header above is required;
without it the server correctly returns `406 Not Acceptable`, which is a different, also-expected
error from the browser-`GET` one above.

---

## Why this exists

Course-completion lists and keyword-matched resumes are a weak signal of what someone actually
knows. This project builds a real knowledge graph from primary sources (not self-reported
summaries), embeds every entry with a real embedding model, and exposes it as a queryable MCP
tool — so "do I have Django experience?" gets answered by walking real evidence (a project's
README, a course completion, an endorsement) with a similarity score attached, not a guess.

## Architecture

```
ingest/            Source-specific scripts → common schema
                    {title, type, provider, date, description, domain_tags}
corpus/             Generated markdown, one subfolder per source (not tracked — see Privacy below)
graph.json          Extracted nodes/edges from corpus/ (not tracked)
mcp/                Public MCP server (Cloudflare Worker) — semantic search, no auth
mcp-private/        Private MCP server — same search, bearer-token gated, adds
                    signal-only evidence (job applications, career interests)
```

**Ingestion sources**: LinkedIn (via the [Member Data Portability
API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/overview), EU-only —
see [`docs/linkedin-connector-notes.md`](docs/linkedin-connector-notes.md) for notes on the
manual-export alternative for other regions), GitHub (via the `gh` CLI, excluding forks — a fork
is evidence of browsing, not building), DataCamp/edX/Skilljar course completions, Gmail
(completion emails from other platforms), and sibling project repos (auto-discovered, evidenced
via README + tracked filenames + a keyword pass, not self-reported).

`ingest/merge.py` deduplicates across sources (idempotent — safe to re-run). `ingest/build_graph.py`
extracts nodes/edges from `corpus/` frontmatter into `graph.json`.

## The RAG layer

Both `mcp/` and `mcp-private/` are stateless Cloudflare Workers (`createMcpHandler`, no Durable
Object) that embed every corpus entry with Workers AI (`@cf/baai/bge-base-en-v1.5`, 768-dim) at
export time, and embed the query string at request time, then rank by cosine similarity. Two MCP
tools are exposed: `query_knowhow(topic)` for semantic search, and `list_by_type(type)` for a
plain listing. The private server additionally tags every result with an `evidence_tier`
(`demonstrated` vs. `signal_only`), so a job application or career-interest entry can never be
mistaken for proof of a skill.

## Privacy design

`corpus/` and `graph.json` are never public — no public-facing code reads them directly. The
**only** sanctioned public data source is `mcp/public_entries.json`, built by
`ingest/build_public_export.py` via a **fail-closed allowlist**: only explicitly listed corpus
categories (courses, projects, certifications, education, endorsements, positions, profile,
recommendations, articles) get exported. A new corpus category is excluded by default until
someone deliberately adds it to the allowlist — the same discipline that keeps job applications
and career-interest data out of the public server entirely; that data only exists in
`mcp-private/`, gated behind a bearer token, and is never committed to this repo either (see
`.gitignore`).

## Career-agent tooling

A second layer built on top of the same corpus: `ingest/analyze_job_postings.py` scores scraped
job postings against known skill coverage using the same embeddings (graded `known`/`peripheral`
similarity, not binary keyword matching), `ingest/cv_tailor.py` matches a posting's requirements
against CV bullets with an explicit two-tier system (exact-term matches vs. semantically-related
matches, the latter always labeled "verify before claiming" rather than asserted), and
`ingest/recommend_courses.py` cross-references course catalogs against coverage gaps.

## Running locally

```bash
pip install -r requirements.txt

# Deduplicate corpus after any ingest run
python ingest/merge.py

# Build graph.json from corpus/
python ingest/build_graph.py

# Run a specific ingest source, e.g.:
python ingest/github_ingest.py
python ingest/linkedin_api_ingest.py --domains PROFILE,POSITIONS,SKILLS
```

Each `ingest/*_ingest.py` script is independent — run whichever sources apply to you. All of them
write markdown into `corpus/<source>/` using the shared schema below.

## Corpus schema

Every markdown file in `corpus/` uses this YAML frontmatter:

```yaml
---
title: "Advanced Python Programming"
type: "course"              # course | certification | position | project | education | ...
provider: "LinkedIn Learning"
date: "2024-01-15"
description: "Free-text summary."
domain_tags:
  - python
  - programming
---
```

## Deploying the MCP servers

```bash
cd mcp && npm install && npm run deploy        # public server
cd mcp-private && npm install && npm run deploy # private server
cd mcp-private && npm run secret                # set PRIVATE_MCP_TOKEN
```

Both need a Cloudflare account with Workers AI access (`[ai]` binding, `remote = true` in
`wrangler.toml`). Rebuilding the embeddings after a corpus change:

```bash
CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_AI_TOKEN=... python ingest/build_public_export.py
CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_AI_TOKEN=... python ingest/build_private_export.py
```

## Adding a new ingest source

1. Create `ingest/<source>_ingest.py` that reads the raw export and writes markdown files to
   `corpus/<source>/` using the schema above.
2. `merge.py` and `build_graph.py` require no changes — they scan `corpus/` generically.
3. If the new category should ever be public, add it deliberately to `ALLOWLIST` in
   `ingest/build_public_export.py` — it's excluded by default otherwise.
