-- Who added each goal, hearing, evidence card and "to prove" item. Set when the
-- item is first saved (and kept across later saves, which rewrite the rows);
-- items that already exist keep a NULL creator and are shown without one. A
-- deleted user's items just lose the name.
--
--   psql case_manager -f db/migrations/048_item_creators.sql

BEGIN;

ALTER TABLE goals ADD COLUMN created_by BIGINT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE hearings ADD COLUMN created_by BIGINT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE allegation_evidence ADD COLUMN created_by BIGINT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE allegation_to_prove ADD COLUMN created_by BIGINT REFERENCES users(id) ON DELETE SET NULL;

COMMIT;
