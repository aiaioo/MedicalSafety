-- Each annexed document's page selection becomes one of three choices: all
-- pages, only the pages the report's snippets came from, or a custom page
-- range typed by the user (e.g. "1-3, 7"). Apply after
-- 009_annexure_all_pages_per_document.sql:
--
--   psql case_manager -f db/migrations/010_annexure_page_mode.sql

BEGIN;

ALTER TABLE report_annexure_documents
    ADD COLUMN page_mode  TEXT NOT NULL DEFAULT 'all' CHECK (page_mode IN ('all', 'snippets', 'custom')),
    ADD COLUMN page_range TEXT NOT NULL DEFAULT '';

UPDATE report_annexure_documents SET page_mode = CASE WHEN all_pages THEN 'all' ELSE 'snippets' END;

ALTER TABLE report_annexure_documents DROP COLUMN all_pages;

COMMIT;
