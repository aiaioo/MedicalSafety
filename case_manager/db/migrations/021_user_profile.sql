-- Personal details on the account page: name, location and a photo.
--
--   psql case_manager -f db/migrations/021_user_profile.sql

BEGIN;

ALTER TABLE users ADD COLUMN full_name  TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN city       TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN country    TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN photo      BYTEA;  -- re-encoded JPEG, see app.py

COMMIT;
