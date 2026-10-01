-- Email verification and password-reset links. Apply after 026_report_images.sql:
--
--   psql case_manager -f db/migrations/027_email_verification.sql
--
-- users.email_verified_at is set once the user follows the link emailed to
-- them (or resets their password through one). Everyone who already has an
-- account is marked verified as of this migration.
--
-- email_tokens holds the links' one-time tokens -- only their SHA-256, like
-- user_sessions, so a database leak doesn't hand out working links. A token
-- is deleted when used; a user has at most one live token per purpose.

BEGIN;

ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMPTZ;
UPDATE users SET email_verified_at = now();

CREATE TABLE email_tokens (
    token_hash  TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose     TEXT NOT NULL CHECK (purpose IN ('verify', 'reset')),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX email_tokens_user_idx ON email_tokens (user_id, purpose);

COMMIT;
