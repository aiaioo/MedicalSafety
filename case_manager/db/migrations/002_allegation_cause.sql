-- Every allegation belongs to a cause (as a case already does), so the
-- allegations workspace can list all of a cause's allegations whether or not
-- they are linked to a case. Apply after 001_users_and_access.sql:
--
--   psql case_manager -f db/migrations/002_allegation_cause.sql
--
-- Existing allegations are put under the cause of the first case they link
-- to; any linked to no case (so with no known cause) go under the default
-- cause of the earliest-created user -- the seed account -- or, if that user
-- has none, the earliest-created cause. (If there are allegations but no
-- cause at all, the migration stops rather than invent one.)

BEGIN;

ALTER TABLE allegations ADD COLUMN cause_id TEXT REFERENCES causes(id) ON DELETE RESTRICT;

UPDATE allegations a SET cause_id = (
    SELECT c.cause_id FROM allegation_cases ac JOIN cases c ON c.id = ac.case_id
    WHERE ac.allegation_id = a.id ORDER BY ac.position LIMIT 1
);

UPDATE allegations SET cause_id = COALESCE(
    (SELECT default_cause_id FROM users ORDER BY id LIMIT 1),
    (SELECT id FROM causes ORDER BY created_at, id LIMIT 1)
)
WHERE cause_id IS NULL;

ALTER TABLE allegations ALTER COLUMN cause_id SET NOT NULL;
CREATE INDEX allegations_cause_id_idx ON allegations (cause_id);

COMMIT;
