-- Images inserted into a report from the editor's Image button (as opposed
-- to annotation snippets, which live with their source document). Stored
-- the same way article_images are -- BYTEA on their own row, compressed on
-- upload -- and deleted along with the report.
--
--   psql case_manager -f db/migrations/026_report_images.sql

BEGIN;

CREATE TABLE report_images (
    id            TEXT PRIMARY KEY,               -- short hex id, embedded in its serving URL
    report_id     TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    content_type  TEXT NOT NULL,
    data          BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX report_images_report_id_idx ON report_images (report_id);

COMMIT;
