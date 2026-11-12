-- A hearing's "Order text": what the case tracker (case_progress_crawler) holds about the hearing -- the outcome or
-- order, and the next date -- kept in a field of its own and refreshed from the tracker, so the user's own summary
-- of the hearing is never overwritten. Encrypted like the other free-text columns.
--
-- cases.tracker_dates: the hearing dates (a JSON list, encrypted) the tracker has already been consulted about, so
-- that a hearing card the user deletes is not made again by the next refresh.
--
-- cases.hearings_edited: whether the user has changed a hearing card since the tracker's hearings were pulled in; the
-- "Delete all hearings" button (for a pull of the wrong case's hearings) is offered only until then.
--
--   psql case_manager -f db/migrations/049_hearing_order_text.sql

BEGIN;

ALTER TABLE hearings ADD COLUMN order_text TEXT NOT NULL DEFAULT '';
ALTER TABLE cases ADD COLUMN tracker_dates TEXT NOT NULL DEFAULT '';
ALTER TABLE cases ADD COLUMN hearings_edited BOOLEAN NOT NULL DEFAULT false;

COMMIT;
