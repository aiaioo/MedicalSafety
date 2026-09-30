-- Whether a report's annexure shows (and its PDF download burns in) the
-- annotations drawn on the annexed documents. Off by default so existing
-- annexures are unchanged. Apply after 011_annexure_page_numbers.sql:
--
--   psql case_manager -f db/migrations/012_annexure_include_annotations.sql

BEGIN;

ALTER TABLE reports
    ADD COLUMN annexure_include_annotations BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
