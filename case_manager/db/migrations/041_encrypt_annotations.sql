-- Annotation shapes (which can carry typed text) are stored encrypted
-- (encryptor.py) in document_annotations.shapes_enc instead of as JSONB in
-- shapes. Existing rows are converted by db/encrypt_annotations.py, which
-- also empties shapes and deletes rows that held no shapes at all (a page
-- with no annotations has no row); deploy/remote-deploy.sh runs it straight
-- after this. Apply with:
--
--   psql case_manager -f db/migrations/041_encrypt_annotations.sql
--   python db/encrypt_annotations.py
--
-- document_annotations.shapes can be dropped in a later migration once every
-- database has been converted.

BEGIN;

ALTER TABLE document_annotations ADD COLUMN shapes_enc BYTEA;

COMMIT;
