-- "Include all pages" becomes a per-document choice in the annexure instead
-- of one setting per report. Apply after 008_report_annexures.sql:
--
--   psql case_manager -f db/migrations/009_annexure_all_pages_per_document.sql

BEGIN;

ALTER TABLE report_annexure_documents ADD COLUMN all_pages BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE report_annexure_documents rad SET all_pages = r.annexure_all_pages
FROM reports r WHERE r.id = rad.report_id;

ALTER TABLE reports DROP COLUMN annexure_all_pages;

COMMIT;
