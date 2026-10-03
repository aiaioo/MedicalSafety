-- Annexure "List of Documents" is on by default for new reports (existing
-- reports keep their current setting). Apply after 037_user_report_sections.sql:
--
--   psql case_manager -f db/migrations/038_annexure_list_of_documents_default_on.sql

BEGIN;

ALTER TABLE reports
    ALTER COLUMN annexure_list_of_documents SET DEFAULT TRUE;

COMMIT;
