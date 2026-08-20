-- Referral ("waive credit") tracking. referred_by is captured and validated at upload
-- time (see site/src/index.ts's /api/upload handler) against subscribers.public_slug or
-- .private_slug. referral_credits is a ledger, not a bare counter, keyed by Stripe
-- checkout session id so a redelivered webhook can't double-credit the same referral.
-- Nothing in this schema auto-applies a credit -- applied_at/notified_at are set by hand
-- (see .github/scripts, once the corresponding scripts land) once a human has actually
-- pushed the Stripe coupon / sent the notification.
ALTER TABLE upload_invites ADD COLUMN referred_by TEXT;

CREATE TABLE referral_credits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  beneficiary_email TEXT NOT NULL,
  reason TEXT NOT NULL,                     -- 'referrer_bonus' | 'referred_new_user'
  checkout_session_id TEXT NOT NULL UNIQUE,
  credited_at TEXT NOT NULL,
  applied_at TEXT,
  notified_at TEXT
);
