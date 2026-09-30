-- Whether the user has turned on "View advanced features" (the Causes, Cases
-- and Allegations sections on the home page, and the Allegations link in the
-- Reports title bar). Per user; off by default. Previously kept only in the
-- browser's localStorage.
--
--   psql case_manager -f db/migrations/014_user_show_advanced.sql

BEGIN;

ALTER TABLE users ADD COLUMN show_advanced BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
