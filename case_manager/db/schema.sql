-- ---------------------------------------------------------------------------
-- PostgreSQL schema for case_manager's application data.
--
-- Scope: everything currently under storage/ EXCEPT storage/cache/ (which
-- holds transformed docx->pdf renders, purely a derived cache of an uploaded
-- document and never read back as data). The uploaded source files
-- themselves (documents/*.pdf, *.docx, *.doc) and the derived snippet PNGs
-- (storage/snippets/**/*.png) stay on disk (or, later, in object storage);
-- only their identifying metadata lives here. See db/storage_backend.py for
-- how a row's stable columns are turned into an actual file location -- no
-- filesystem path or URL is ever stored in this database, precisely so the
-- storage backend can be swapped (local disk today, S3 tomorrow) without a
-- data migration.
--
-- Source-of-truth mapping (storage/<dir> JSON shape -> tables below):
--   doc_meta/<id>.json                 -> documents
--   annotations/<id>__<type>.json      -> document_annotations
--   snippets/<id>__<type>.json         -> snippets
--   reports/<id>.json                  -> reports
--   cases/<id>.json                    -> cases, hearings, hearing_documents
--   causes/<id>.json, _last_used.json  -> causes, goals, goal_cases, app_settings
--   allegations/<id>.json,
--   allegations/allegation_order.json  -> allegations, allegation_evidence,
--                                          allegation_to_prove,
--                                          allegation_to_prove_evidence,
--                                          allegation_cases
-- ---------------------------------------------------------------------------

BEGIN;

-- ===========================================================================
-- Uploaded source documents (the PDF/Word files themselves live in
-- documents/, or later in object storage -- this row is just their
-- metadata + identity). doc_type mirrors the on-disk extension; app code
-- collapses 'doc'/'docx' to a single "docx" family when it needs to decide
-- whether a conversion-to-PDF cache entry applies.
-- ===========================================================================
CREATE TABLE documents (
    id          TEXT PRIMARY KEY,               -- doc_id; matches the uploaded file's stem
    doc_type    TEXT NOT NULL CHECK (doc_type IN ('pdf', 'docx', 'doc')),
    title       TEXT NOT NULL DEFAULT '',        -- user-editable display title; defaults to id in the app
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-page freehand/rect annotation overlays burned into a PDF render on
-- demand (never mutate the source file). One row per annotated page; a page
-- with no annotations simply has no row.
CREATE TABLE document_annotations (
    id           BIGSERIAL PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number  INTEGER NOT NULL CHECK (page_number > 0),
    -- Array of shapes: {"kind":"rect","color":"#rrggbb","x":.., "y":.., "w":.., "h":..}
    --              or  {"kind":"freehand","color":"#rrggbb","points":[[x,y],...]}
    -- (x/y/w/h/points are all fractions of the page, 0..1) -- kept as JSONB
    -- since shape kinds and their fields vary and are drawn generically.
    shapes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number)
);

-- Cropped page snippets (evidence thumbnails) extracted from a document page.
-- The crop rect is structured/queryable data, so it gets real columns; the
-- PNG bytes stay on disk under a key derived from (document_id, doc_type,
-- filename) via db/storage_backend.py:snippet_storage_key -- never stored
-- here as a path.
CREATE TABLE snippets (
    id           TEXT PRIMARY KEY,               -- short hex id, also embedded in the PNG filename
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number  INTEGER NOT NULL CHECK (page_number > 0),
    filename     TEXT NOT NULL,                  -- e.g. "p1_b494c9abc314.png"; combined with document_id/doc_type
                                                   -- by the storage key function to locate the file
    rect_x       DOUBLE PRECISION NOT NULL CHECK (rect_x BETWEEN 0 AND 1),
    rect_y       DOUBLE PRECISION NOT NULL CHECK (rect_y BETWEEN 0 AND 1),
    rect_w       DOUBLE PRECISION NOT NULL CHECK (rect_w > 0),
    rect_h       DOUBLE PRECISION NOT NULL CHECK (rect_h > 0),
    annotated    BOOLEAN NOT NULL DEFAULT FALSE,  -- true if the crop was baked with annotations burned in
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX snippets_document_id_idx ON snippets (document_id);

-- ===========================================================================
-- Reports: Tiptap/ProseMirror documents authored in the report editor.
-- The document tree is arbitrary, deeply nested, editor-owned JSON, so it
-- stays JSONB wholesale rather than being decomposed into rows.
-- ===========================================================================
CREATE TABLE reports (
    id                 TEXT PRIMARY KEY,          -- "<slugified-name>-<hex6>"
    name               TEXT NOT NULL,
    -- NULL means "predates the Tiptap/ProseMirror JSON format" (an old
    -- report saved before the editor stored structured JSON) -- the report
    -- editor/export routes detect this and tell the user to open and
    -- re-save it, so NULL is preserved rather than coerced to an empty doc.
    doc                JSONB,
    source_document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
    -- Small, evolving editor-settings blobs (page-numbering options in
    -- particular have grown new keys over time) -- JSONB rather than columns
    -- so the report editor can add fields without a migration.
    -- Defaults match REPORT_DEFAULT_MARGINS / REPORT_DEFAULT_PAGE_NUMBERS in app.py.
    margins            JSONB NOT NULL DEFAULT '{"left":36,"right":36,"header":46,"footer":46}'::jsonb,
    page_numbers       JSONB NOT NULL DEFAULT '{"position":"top-center","skip":0,"font":"Arial","fontSize":11}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- Causes workspace: a cause holds an ordered list of goals; a goal can link
-- to zero or more cases (many-to-many).
-- ===========================================================================
CREATE TABLE causes (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========================================================================
-- Cases workspace: a case belongs to exactly one cause (mandatory
-- association -- see commit da6ff0b) and owns an ordered list of hearings,
-- each of which can link previously-uploaded documents as "submitted" or
-- "received" at that hearing.
--
-- The app currently repairs a case whose cause was deleted by re-pointing it
-- at the most-recently-updated remaining cause (or a fresh "General" cause);
-- ON DELETE RESTRICT means that reassignment must keep happening at the
-- application layer *before* a cause row is deleted, which is also how the
-- existing app.py behaves (it never lets cause_id go null).
-- ===========================================================================
CREATE TABLE cases (
    id          TEXT PRIMARY KEY,
    cause_id    TEXT NOT NULL REFERENCES causes(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL DEFAULT '',
    court       TEXT NOT NULL DEFAULT '',
    case_number TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX cases_cause_id_idx ON cases (cause_id);

CREATE TABLE hearings (
    id            TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL DEFAULT 0,      -- preserves the hearings list's display order within a case
    hearing_date  TEXT NOT NULL DEFAULT '',         -- free text as entered by the user, not a real DATE (format isn't enforced)
    title         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX hearings_case_id_idx ON hearings (case_id);

CREATE TABLE hearing_documents (
    id           TEXT PRIMARY KEY,
    hearing_id   TEXT NOT NULL REFERENCES hearings(id) ON DELETE CASCADE,
    direction    TEXT NOT NULL CHECK (direction IN ('submitted', 'received')),
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX hearing_documents_hearing_id_idx ON hearing_documents (hearing_id);
CREATE INDEX hearing_documents_document_id_idx ON hearing_documents (document_id);

CREATE TABLE goals (
    id           TEXT PRIMARY KEY,
    cause_id     TEXT NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0        -- preserves the goals list's display order within a cause
);
CREATE INDEX goals_cause_id_idx ON goals (cause_id);

CREATE TABLE goal_cases (
    goal_id   TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    case_id   TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (goal_id, case_id)
);

-- ===========================================================================
-- Allegations: a title/description plus three sub-lists --
--   - inculpatory / exculpatory evidence items (each optionally linked to one report)
--   - "things to prove" items, each optionally linked to one or more of the
--     allegation's own evidence items
-- and a many-to-many link to cases. Global display order across all
-- allegations (previously allegations/allegation_order.json) is now the
-- order_index column instead of a separate ordering file.
-- ===========================================================================
CREATE TABLE allegations (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    order_index  INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX allegations_order_idx ON allegations (order_index);

CREATE TABLE allegation_evidence (
    id            TEXT PRIMARY KEY,
    allegation_id TEXT NOT NULL REFERENCES allegations(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('inculpatory', 'exculpatory')),
    text          TEXT NOT NULL DEFAULT '',
    report_id     TEXT REFERENCES reports(id) ON DELETE SET NULL,
    position      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX allegation_evidence_allegation_id_idx ON allegation_evidence (allegation_id);

CREATE TABLE allegation_to_prove (
    id            TEXT PRIMARY KEY,
    allegation_id TEXT NOT NULL REFERENCES allegations(id) ON DELETE CASCADE,
    title         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    position      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX allegation_to_prove_allegation_id_idx ON allegation_to_prove (allegation_id);

CREATE TABLE allegation_to_prove_evidence (
    to_prove_id  TEXT NOT NULL REFERENCES allegation_to_prove(id) ON DELETE CASCADE,
    evidence_id  TEXT NOT NULL REFERENCES allegation_evidence(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (to_prove_id, evidence_id)
);

-- A "to prove" item may only link to evidence belonging to the *same*
-- allegation (this is exactly what app.py's sanitize_to_prove_list enforces
-- via its valid_evidence_ids set) -- a trigger keeps that invariant even if
-- a future caller bypasses the app layer.
CREATE FUNCTION check_to_prove_evidence_same_allegation() RETURNS TRIGGER AS $$
BEGIN
    IF (SELECT allegation_id FROM allegation_to_prove WHERE id = NEW.to_prove_id)
       IS DISTINCT FROM
       (SELECT allegation_id FROM allegation_evidence WHERE id = NEW.evidence_id) THEN
        RAISE EXCEPTION 'allegation_to_prove_evidence: to_prove % and evidence % belong to different allegations',
            NEW.to_prove_id, NEW.evidence_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER allegation_to_prove_evidence_same_allegation
    BEFORE INSERT OR UPDATE ON allegation_to_prove_evidence
    FOR EACH ROW EXECUTE FUNCTION check_to_prove_evidence_same_allegation();

CREATE TABLE allegation_cases (
    allegation_id  TEXT NOT NULL REFERENCES allegations(id) ON DELETE CASCADE,
    case_id        TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (allegation_id, case_id)
);
CREATE INDEX allegation_cases_case_id_idx ON allegation_cases (case_id);

-- ===========================================================================
-- Miscellaneous singleton app state (previously storage/causes/_last_used.json).
-- The "id boolean primary key check (id)" trick guarantees at most one row.
-- ===========================================================================
CREATE TABLE app_settings (
    id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    last_used_cause_id  TEXT REFERENCES causes(id) ON DELETE SET NULL
);
INSERT INTO app_settings (id, last_used_cause_id) VALUES (TRUE, NULL);

COMMIT;
