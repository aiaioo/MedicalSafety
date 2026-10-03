-- Turn the annexure "List of Documents" on for every existing report (new
-- reports already default to on, see 038). Apply after 038:
--
--   psql case_manager -f db/migrations/039_annexure_list_of_documents_enable_existing.sql

BEGIN;

UPDATE reports SET annexure_list_of_documents = TRUE;

COMMIT;
