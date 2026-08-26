# PersonalKnowHow MCP — private-tier customer template

Template for a customer's private-tier MCP Worker. `src/index.ts`'s `CUSTOMER_LABEL` starts as
`"CHANGE_ME"` — clone this directory per customer, set `CUSTOMER_LABEL` to their
`subscribers.public_slug`/`.private_slug`, drop in that customer's real `entries.json` +
`embeddings.json` (built by `ingest/build_private_export.py`), match the Worker name in
`wrangler.toml`/`package.json`, then `wrangler deploy`.

Bearer-token-gated (`Authorization: Bearer <PRIVATE_MCP_TOKEN>`, `401` without it) and gated a
second way in D1 — `deleted_at`/`subscription_status` on the customer's `upload_invites` row
(see `src/index.ts`'s `handle()`) — so a "delete my data" request or a lapsed subscription cuts
access even though the data is baked into the deployed bundle.

Every result includes `evidence_tier` (`"demonstrated"` vs `"signal_only"`) — this tier's export
includes career-interest/job-application "signal" evidence alongside real demonstrated
experience, so this field is how a caller tells the two apart.

## Tools

Same four tools as `mcp-private/` (that directory's `README.md` has real worked examples for all
four, directly reusable here since the tool bodies are byte-for-byte the same — only the
per-customer data differs):

- **`query_knowhow(topic)`** — semantic search, ranked, capped at 10.
- **`list_by_type(type)`** — complete, exact set for one type, no cap.
- **`related_entries(id)`** — entries sharing a tag or provider with a given entry, capped at
  15/group.
- **`skill_evidence(tag)`** — exact tag match, uncapped, grouped by `evidence_tier` then `type`
  with real counts. The tool for "is there ANY demonstrated evidence for X" — `query_knowhow`
  can't answer that because similarity search always ranks something, even at zero real matches.
