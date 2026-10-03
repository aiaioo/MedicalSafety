-- Reports page: each user's section layout ([{id, name, reports: [id, ...]}],
-- in display order; reports not listed fall into the implicit "General"
-- section). Apply after 036_case_party_in_person.sql:
--
--   psql case_manager -f db/migrations/037_user_report_sections.sql

BEGIN;

ALTER TABLE users
    ADD COLUMN report_sections JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
