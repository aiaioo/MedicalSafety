-- Hearing alerts: an email to the people on a case the first time the case tracker (case_progress_crawler) has
-- the court's record of a hearing (see hearing_alerts.py).
--
-- hearing_alerts: one row per (case, hearing date) already dealt with, so a hearing is announced once. A case's
-- first check records every hearing it already has with emailed = false (the "baseline": a user who adds a case
-- is not sent its whole history); hearings found after that are emailed, and the row says when.
--
-- users.hearing_alerts: whether the user wants these emails (on by default; a checkbox on the home page).
--
--   psql case_manager -f db/migrations/052_hearing_alerts.sql

BEGIN;

CREATE TABLE hearing_alerts (
    case_id       TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    hearing_date  DATE NOT NULL,
    emailed       BOOLEAN NOT NULL DEFAULT false,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, hearing_date)
);

-- A case's baseline is taken once: its row here says it has been.
CREATE TABLE hearing_alert_baselines (
    case_id     TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    taken_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE users ADD COLUMN hearing_alerts BOOLEAN NOT NULL DEFAULT true;

COMMIT;
