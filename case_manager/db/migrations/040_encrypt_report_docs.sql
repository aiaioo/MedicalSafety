-- Report text is stored encrypted (encryptor.py) in reports.doc_enc instead of
-- as JSONB in reports.doc. thumbnail_url holds the report's first image (it
-- used to be read out of the JSONB by the reports list). Existing rows are
-- converted by db/encrypt_report_docs.py, which also NULLs reports.doc;
-- deploy/remote-deploy.sh runs it straight after this. NULL doc_enc with NULL
-- doc still means "old format, re-save". Apply with:
--
--   psql case_manager -f db/migrations/040_encrypt_report_docs.sql
--   python db/encrypt_report_docs.py
--
-- reports.doc can be dropped in a later migration once every database has
-- been converted.

BEGIN;

ALTER TABLE reports
    ADD COLUMN doc_enc       BYTEA,
    ADD COLUMN thumbnail_url TEXT;

COMMIT;
