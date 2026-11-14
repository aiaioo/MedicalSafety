-- Splitting a long document into shorter ones (the annotations page's
-- File > Split document...). A split is a journalled, crash-recoverable job:
--
--   building   the new PDFs and snippet images are being written to storage.
--              The source document is locked (documents.splitting) -- no
--              annotation, snippet, title or delete changes are accepted.
--   committed  the database now points at the new documents (annotations,
--              snippets, reports, annexures and hearings were all moved in one
--              transaction); only deleting the old snippet image files is left.
--   done       finished. The row stays, because it maps the old document's
--              pages and snippet images to the new ones, which keeps an old
--              link (a report still open in someone's browser) working.
--   cancelled  rolled back; the partly written files were removed.
--
-- Apply after 050_drop_goals_and_cause_description.sql:
--
--   psql case_manager -f db/migrations/051_document_splits.sql

BEGIN;

ALTER TABLE documents ADD COLUMN splitting BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE document_splits (
    id           TEXT PRIMARY KEY,
    -- No foreign key: the row outlives the source document if it is deleted later.
    document_id  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('building', 'committed', 'done', 'cancelled')),
    -- {"page_count": N, "segments": [{"id": new document id, "start": first source page,
    --   "end": last source page, "title": ...}, ...]}
    plan         JSONB NOT NULL,
    created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A document is split at most once, and by at most one job at a time.
CREATE UNIQUE INDEX document_splits_document_idx ON document_splits (document_id) WHERE status <> 'cancelled';

COMMIT;
