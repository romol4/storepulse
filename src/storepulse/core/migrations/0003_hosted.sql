-- 0003: hosted-mode additions (docs/SPEC.md, Storage). Never edit; add a new migration instead.
--
-- Applied to every database, local ones included; local mode leaves these tables empty
-- and never writes the apps columns below.

-- Secrets encrypted with AES-GCM under a key derived from STOREPULSE_MASTER_KEY.
-- fingerprint is keyed (HMAC), so the database alone can't be used to test guesses.
CREATE TABLE secrets (
  name        TEXT PRIMARY KEY,
  ciphertext  BLOB NOT NULL,
  nonce       BLOB NOT NULL,
  fingerprint TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE users (
  id              INTEGER PRIMARY KEY,
  email           TEXT UNIQUE NOT NULL,
  password_hash   TEXT NOT NULL,
  totp_secret_enc BLOB
);

-- Non-secret configuration, one row per dotted key, JSON-encoded.
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- Server-side login sessions. Only a hash of the cookie token is stored.
-- totp_pending = 1 while a password was accepted but the second factor wasn't yet.
CREATE TABLE sessions (
  token_hash   TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token   TEXT NOT NULL,
  totp_pending INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);

ALTER TABLE apps ADD COLUMN display_name TEXT;
ALTER TABLE apps ADD COLUMN pair_key TEXT;
ALTER TABLE apps ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apps ADD COLUMN pair_source TEXT CHECK (pair_source IN ('auto', 'user'));
