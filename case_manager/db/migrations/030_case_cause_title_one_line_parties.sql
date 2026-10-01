-- Cause title option: show each side's parties on one line, as
-- "<first party> and Ors.", instead of enumerating them all.
--
--   psql case_manager -f db/migrations/030_case_cause_title_one_line_parties.sql

BEGIN;

ALTER TABLE cases ADD COLUMN cause_title_one_line_parties BOOLEAN NOT NULL DEFAULT false;

COMMIT;
