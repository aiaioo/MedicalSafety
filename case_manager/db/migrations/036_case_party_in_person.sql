-- Case details: whether the user is a party in person (appearing without a
-- lawyer). Apply after 035_case_role.sql:
--
--   psql case_manager -f db/migrations/036_case_party_in_person.sql

BEGIN;

ALTER TABLE cases
    ADD COLUMN party_in_person BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
