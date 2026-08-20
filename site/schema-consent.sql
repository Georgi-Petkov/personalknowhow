-- Consent capture for the 90-day fixed retention policy: data is kept for 90
-- days from last upload (used_at), used only to power/update the customer's
-- MCP server, never shared. consent_given_at is set alongside used_at when a
-- customer submits their upload with the consent checkbox checked. See
-- site/src/index.ts's /api/upload handler and .github/scripts/retention_expiry.py.
ALTER TABLE upload_invites ADD COLUMN consent_given_at TEXT;
