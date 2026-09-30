-- Records which user created each snippet. Optional: snippets that predate
-- this migration, and snippets whose creator's account was deleted, have
-- NULL (shown to the source's owner as "Unknown user"). Only the owner sees
-- it, in the side panel; it is never exported. Apply after
-- 016_inherited_access.sql:
--
--   psql case_manager -f db/migrations/017_snippet_created_by.sql

BEGIN;

ALTER TABLE snippets
    ADD COLUMN created_by BIGINT REFERENCES users(id) ON DELETE SET NULL;

COMMIT;
