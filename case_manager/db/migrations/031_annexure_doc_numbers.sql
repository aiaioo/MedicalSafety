-- Optional per-document labels ("Annexure P-1", "Document D1", ...) stamped in
-- the top margin of each annexed document's first page. Off by default so
-- existing annexures are unchanged. Apply after 030_case_cause_title_one_line_parties.sql:
--
--   psql case_manager -f db/migrations/031_annexure_doc_numbers.sql

BEGIN;

ALTER TABLE reports
    ADD COLUMN annexure_doc_numbers JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
