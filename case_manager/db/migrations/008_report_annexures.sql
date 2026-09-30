-- An annexure is the set of reference pages (from source documents) that is
-- added behind a report for submission: an ordered list of documents per
-- report, plus whether every page of them is included or only the pages the
-- report's snippets were taken from. Apply after 007_report_snippets.sql:
--
--   psql case_manager -f db/migrations/008_report_annexures.sql

BEGIN;

ALTER TABLE reports ADD COLUMN annexure_all_pages BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE report_annexure_documents (
    report_id    TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    PRIMARY KEY (report_id, document_id)
);

COMMIT;
