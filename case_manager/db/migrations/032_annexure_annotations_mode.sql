-- Annexure annotations become a three-way choice: 'none', 'blackouts' or 'all'
-- (was the boolean annexure_include_annotations). Existing reports keep their
-- setting: TRUE becomes 'all'. Apply after 031_annexure_doc_numbers.sql:
--
--   psql case_manager -f db/migrations/032_annexure_annotations_mode.sql

BEGIN;

ALTER TABLE reports
    ADD COLUMN annexure_annotations TEXT NOT NULL DEFAULT 'none'
        CHECK (annexure_annotations IN ('none', 'blackouts', 'all'));

UPDATE reports SET annexure_annotations = 'all' WHERE annexure_include_annotations;

COMMIT;
