-- Optional free-text description of a source document, entered on the
-- annexures and annotations pages; it will feed the annexure's List of
-- Documents. Apply after 032_annexure_annotations_mode.sql:
--
--   psql case_manager -f db/migrations/033_document_description.sql

BEGIN;

ALTER TABLE documents
    ADD COLUMN description TEXT NOT NULL DEFAULT '';

COMMIT;
