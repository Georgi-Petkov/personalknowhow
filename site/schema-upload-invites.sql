CREATE TABLE IF NOT EXISTS upload_invites (
  token      TEXT PRIMARY KEY,
  label      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  used_at    TEXT
);
