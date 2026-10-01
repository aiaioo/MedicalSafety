-- A case's cause title is generated from a template, but can be edited by
-- hand on the case details page. The hand-edited version is stored here as a
-- ProseMirror doc (JSON text); NULL means "still generated from the template".
--
--   psql case_manager -f db/migrations/029_case_cause_title_doc.sql

BEGIN;

ALTER TABLE cases ADD COLUMN cause_title_doc TEXT;

COMMIT;
