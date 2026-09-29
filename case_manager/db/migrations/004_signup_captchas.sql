-- Sign-up captcha challenges. Apply after 003_default_cause_for_every_user.sql:
--
--   psql case_manager -f db/migrations/004_signup_captchas.sql
--
-- One row per captcha image shown on the sign-up form. The row is deleted the
-- first time it is answered (right or wrong), so a solved captcha can't be
-- replayed; unanswered ones expire and are swept out as new ones are created.

BEGIN;

CREATE TABLE signup_captchas (
    id          TEXT PRIMARY KEY,
    answer      TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);

COMMIT;
