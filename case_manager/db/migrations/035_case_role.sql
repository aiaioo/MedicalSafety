-- Case role: the party role the user's side plays in the case (Complainant,
-- Plaintiff, ...). Apply after 034_annexure_list_of_documents.sql:
--
--   psql case_manager -f db/migrations/035_case_role.sql

BEGIN;

ALTER TABLE cases
    ADD COLUMN case_role TEXT NOT NULL DEFAULT 'Complainant';

COMMIT;
