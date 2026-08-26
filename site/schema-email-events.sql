-- Resend email-engagement events (sent/delivered/opened/clicked/bounced), captured via
-- POST /api/resend-webhook (see site/src/index.ts) instead of living only in Resend's
-- dashboard. svix_id is the dedup key -- Resend retries with backoff on any non-200
-- response, and INSERT OR IGNORE (in the webhook handler) means a redelivered event is
-- a silent no-op instead of a duplicate row, the same pattern referral_credits already
-- uses to dedup on Stripe's checkout session id. event_type is stored as-received, not
-- restricted to a fixed enum -- forward-compatible with event types beyond the 5 this
-- Worker is actually subscribed to. upload_token correlates an event back to the
-- upload_invites row it belongs to: primarily via Resend's `tags` field (set on send by
-- send_invite.py/send_payment_link.py, present on every event type), falling back to
-- parsing the clicked link's own ?token= for email.clicked events sent before tagging
-- existed. raw_payload keeps the full JSON body as a cheap forward-compat safety net
-- for anything not modeled in its own column.
CREATE TABLE email_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  svix_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  email_id TEXT NOT NULL,
  recipient TEXT,
  upload_token TEXT,
  clicked_url TEXT,
  bounce_type TEXT,
  bounce_message TEXT,
  event_created_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  raw_payload TEXT NOT NULL
);
CREATE INDEX idx_email_events_token ON email_events(upload_token);
CREATE INDEX idx_email_events_email_id ON email_events(email_id);
