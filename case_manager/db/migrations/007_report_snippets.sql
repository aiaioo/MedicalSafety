-- Records which snippets each report embeds, so deleting a snippet can check
-- "is it used in a report?" with an index lookup instead of scanning every
-- report's JSON. Apply after 006_general_cause_for_unassociated.sql:
--
--   psql case_manager -f db/migrations/007_report_snippets.sql
--
-- save_report keeps the table in step from now on; this backfills it from the
-- reports that already exist (one last full scan).

BEGIN;

CREATE TABLE report_snippets (
    report_id   TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    snippet_id  TEXT NOT NULL REFERENCES snippets(id) ON DELETE CASCADE,
    PRIMARY KEY (report_id, snippet_id)
);
CREATE INDEX report_snippets_snippet_id_idx ON report_snippets (snippet_id);

INSERT INTO report_snippets (report_id, snippet_id)
SELECT r.id, s.id
FROM reports r
JOIN snippets s ON position('/media/snippets/' || s.document_id || '/' || s.filename in r.doc::text) > 0;

COMMIT;
