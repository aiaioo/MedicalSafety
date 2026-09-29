-- Per-IP rate limit on sign-up attempts. Apply after 004_signup_captchas.sql:
--
--   psql case_manager -f db/migrations/005_signup_attempts.sql
--
-- One row per accepted sign-up attempt (see auth.py's SIGNUP_ATTEMPT_LIMIT);
-- rows older than the limit's window are swept out as new ones are recorded.

BEGIN;

CREATE TABLE signup_attempts (
    ip            TEXT NOT NULL,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX signup_attempts_ip_idx ON signup_attempts (ip, attempted_at);

COMMIT;
