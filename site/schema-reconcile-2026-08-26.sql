-- RECONCILIATION FILE -- documents columns that already exist in the live production
-- database but were never captured in any committed schema*.sql migration. They were added
-- out-of-band (wrangler CLI one-liners / dashboard console), presumably at the same time as
-- the feature that needed them, but no record of that landed in git until now.
--
-- DO NOT RUN THIS FILE AGAINST PRODUCTION. Every column below already exists live (confirmed
-- via `wrangler d1 execute personalknowhow-waitlist --remote --command "PRAGMA table_info(...)"`
-- on 2026-08-26) -- SQLite's ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" form, so running
-- this would just error on the first statement. This file exists purely so the schema history
-- in git matches reality; treat it as documentation, not a migration to apply.
--
-- Found while reconciling: mcp-customer-template/src/index.ts's own comment already flags
-- customer_label as a column that was tried and abandoned for a real reason -- "one invite row
-- covers two slugs (public+private), so a single customer_label column can't identify which
-- Worker this is" (the exact bug behind the 2026-08-16 incident where a customer_label-keyed
-- access-control query silently matched nothing and failed open). It's listed here for the
-- historical record, not because it's live-used -- nothing currently reads or writes it.

-- upload_invites --------------------------------------------------------------------------

-- Core recipient column -- present from very early on (send_invite.py/send_payment_link.py/
-- send_mcp_ready.py all key off it), but the original schema-upload-invites.sql never included
-- it.
ALTER TABLE upload_invites ADD COLUMN email TEXT;

-- Two-tier MCP delivery: the public (shareable, unauthenticated) and private (bearer-token-
-- gated) connection URLs + the private tier's token, set by hand once a customer's Worker pair
-- is deployed, read by send_mcp_ready.py and site/src/index.ts's /api/status. These supersede
-- schema-upload-invites-v2.sql's singular mcp_url/mcp_token columns, which are dead -- no
-- current code path reads or writes them.
ALTER TABLE upload_invites ADD COLUMN mcp_url_public TEXT;
ALTER TABLE upload_invites ADD COLUMN mcp_url_private TEXT;
ALTER TABLE upload_invites ADD COLUMN mcp_token_private TEXT;

-- Stripe integration -- written by site/src/index.ts's /api/stripe-webhook handler
-- (checkout.session.completed sets all four; customer.subscription.updated/.deleted updates
-- subscription_status only).
ALTER TABLE upload_invites ADD COLUMN payment_confirmed_at TEXT;
ALTER TABLE upload_invites ADD COLUMN stripe_customer_id TEXT;
ALTER TABLE upload_invites ADD COLUMN stripe_subscription_id TEXT;
ALTER TABLE upload_invites ADD COLUMN subscription_status TEXT;

-- Set by process_upload.yml's "Quarantine and record rejection" step when
-- ingest/validate_linkedin_export.py rejects an upload (zip bomb, path traversal, not a real
-- LinkedIn export). Read by site/src/index.ts's /api/status to show the customer why.
ALTER TABLE upload_invites ADD COLUMN upload_rejected_reason TEXT;

-- Set by site/src/index.ts's /api/delete-data handler -- the email the deletion request was
-- confirmed against (distinct from the invite's own `email`, which the requester might not
-- match if deletion is ever handled on someone's behalf -- kept as its own audit field).
ALTER TABLE upload_invites ADD COLUMN deleted_by_email TEXT;

-- Set by .github/scripts/cleanup_digest.py once the Worker(s) for a deleted invite have
-- actually been torn down by hand -- the weekly digest only re-lists invites where this is
-- still NULL despite deleted_at being set.
ALTER TABLE upload_invites ADD COLUMN worker_cleaned_up_at TEXT;

-- Dead/vestigial -- see the file header note above. Kept only because it's a real live column;
-- not written or read by any current code path.
ALTER TABLE upload_invites ADD COLUMN customer_label TEXT;

-- subscribers -------------------------------------------------------------------------------

-- HMAC-derived (site/src/index.ts's deriveSlug(), keyed by SLUG_HMAC_KEY, never the email's
-- local part) customer subdomain slugs, set by /api/waitlist at signup time. The two purposes
-- ("public"/"private") are cryptographically unlinkable from each other by design.
ALTER TABLE subscribers ADD COLUMN public_slug TEXT;
ALTER TABLE subscribers ADD COLUMN private_slug TEXT;
