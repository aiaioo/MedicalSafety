-- Case details: the parties to a case (complainants and respondents, each
-- an ordered list), the court's location, and the cause title generated
-- from an admin-managed template with [COURT_NAME], [COURT_LOCATION],
-- [CASE_NUMBER], [PLAINTIFFS] and [RESPONDENTS] placeholders.
--
--   psql case_manager -f db/migrations/028_case_details.sql

BEGIN;

ALTER TABLE cases
    ADD COLUMN court_location             TEXT NOT NULL DEFAULT '',
    ADD COLUMN cause_title_template_id    TEXT,          -- NULL: the first template
    ADD COLUMN cause_title_font           TEXT NOT NULL DEFAULT '',
    ADD COLUMN cause_title_font_size      INTEGER NOT NULL DEFAULT 0;   -- points; 0: the page default

CREATE TABLE case_parties (
    id        TEXT PRIMARY KEY,
    case_id   TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    side      TEXT NOT NULL CHECK (side IN ('complainant', 'respondent')),
    position  INTEGER NOT NULL DEFAULT 0,
    name      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX case_parties_case_id_idx ON case_parties (case_id);

CREATE TABLE cause_title_templates (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO cause_title_templates (id, name, body) VALUES (
    'consumer-commission',
    'Consumer Dispute Redressal Commission',
    E'BEFORE THE [COURT_NAME] AT [COURT_LOCATION]\n[CASE_NUMBER]\nBETWEEN:\n[PLAINTIFFS]\n\nAND:\n[RESPONDENTS]'
);

COMMIT;
