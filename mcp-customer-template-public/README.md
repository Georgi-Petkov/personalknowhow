# PersonalKnowHow MCP — public-tier customer template

Template for a customer's public-tier MCP Worker — unauthenticated, deployed alongside their
private-tier pair (`mcp-customer-template/`). Clone this directory per customer, drop in that
customer's real `public_entries.json` + `public_embeddings.json` (built by
`ingest/build_public_export.py`), match the Worker name in `wrangler.toml`/`package.json`, then
`wrangler deploy`.

No bearer-token auth (unauthenticated by design) — but still gated in D1 the same way as the
private tier: `deleted_at`/`subscription_status` on the customer's `upload_invites` row (see
`src/index.ts`'s `handle()`) can return `410`/`402` even though this tier has no token check.
Safely public otherwise because career-interest/job-application "signal" evidence is
structurally excluded at export time, not filtered here.

## Tools

Same four tools as `mcp/` (that directory's `README.md` has real worked examples for all four,
directly reusable here since the tool bodies are byte-for-byte the same — only the per-customer
data differs):

- **`query_knowhow(topic)`** — semantic search, ranked, capped at 10.
- **`list_by_type(type)`** — complete, exact set for one type, no cap.
- **`related_entries(id)`** — entries sharing a tag or provider with a given entry, capped at
  15/group.
- **`skill_evidence(tag)`** — exact tag match, uncapped, grouped by `type` with real counts. The
  tool for "how many X has this person actually done" — `query_knowhow` can't answer that
  reliably since similarity ranking has no cap-free "give me every real match" mode.
