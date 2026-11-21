-- Hearing reminders (hearing_alerts.py): an email at 4 am IST two weeks, two days and on the morning of a hearing.
-- One row per reminder sent, so each goes out once per (case, hearing date).
--
--   psql case_manager -f db/migrations/053_hearing_reminders.sql

BEGIN;

CREATE TABLE hearing_reminders (
    case_id       TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    hearing_date  DATE NOT NULL,
    days_before   INTEGER NOT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, hearing_date, days_before)
);

COMMIT;
