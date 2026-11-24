-- New reports' annexures number their pages at the top centre by default (the
-- column defaulted to no numbering). Matches ANNEXURE_DEFAULT_PAGE_NUMBERS in
-- app.py. Existing reports keep their current setting. Apply after
-- 055_annexure_annotations_default_blackouts.sql:
--
--   psql case_manager -f db/migrations/056_annexure_page_numbers_default_top_center.sql

ALTER TABLE reports ALTER COLUMN annexure_page_numbers SET DEFAULT
    '{"position":"top-center","skip":0,"first":1,"font":"Arial","fontSize":11,"shape":"circle","color":"#777777","alternate":true}'::jsonb;
