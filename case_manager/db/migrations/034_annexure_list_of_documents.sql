-- Annexure "List of Documents": an optional unnumbered index page (or pages)
-- placed before the annexed documents. Apply after 033_document_description.sql:
--
--   psql case_manager -f db/migrations/034_annexure_list_of_documents.sql

BEGIN;

ALTER TABLE reports
    ADD COLUMN annexure_list_of_documents BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
