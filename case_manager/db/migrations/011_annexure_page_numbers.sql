-- Page-numbering options for a report's annexure (position, start page, font,
-- font size -- same shape as reports.page_numbers). Defaults to no numbering so
-- existing annexure downloads are unchanged. Apply after
-- 010_annexure_page_mode.sql:
--
--   psql case_manager -f db/migrations/011_annexure_page_numbers.sql

BEGIN;

ALTER TABLE reports
    ADD COLUMN annexure_page_numbers JSONB NOT NULL DEFAULT '{"position":"none","skip":0,"font":"Arial","fontSize":11}'::jsonb;

COMMIT;
