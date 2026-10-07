-- Documents page: each user's section layout ([{id, name, documents: [id, ...]}],
-- in display order; documents not listed fall into the implicit "General"
-- section), like users.report_sections on the reports page. Apply after
-- 043_key_vault_admin.sql:
--
--   psql case_manager -f db/migrations/044_user_document_sections.sql

BEGIN;

ALTER TABLE users
    ADD COLUMN document_sections JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
