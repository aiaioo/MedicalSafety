-- Uploaded files are stored per user: storage/<storage_owner>/documents/,
-- .../snippets/ and .../cache/, where storage_owner is the first 32 hex chars
-- of the SHA-256 of the lowercased email of the user who owns the cause the
-- document belongs to (see db/storage_backend.py's owner_dir_name). It is
-- kept permanently on the user's row (users.storage_dir, set at sign-up) and
-- copied onto the document's row, so later link or email changes never move
-- files.
-- Apply after 012_annexure_include_annotations.sql, then move the existing
-- files to match:
--
--   psql case_manager -f db/migrations/013_document_storage_owner.sql
--   python db/relocate_files.py
--
-- Existing documents get the owner of their earliest-created linked cause
-- (directly, or through a case); failing that, their own owner. A document
-- nobody can reach stays NULL and keeps the flat storage/documents/ layout.

BEGIN;

ALTER TABLE users ADD COLUMN storage_dir TEXT;
UPDATE users SET storage_dir = substr(encode(sha256(convert_to(lower(email), 'UTF8')), 'hex'), 1, 32);
ALTER TABLE users ALTER COLUMN storage_dir SET NOT NULL;
CREATE UNIQUE INDEX users_storage_dir_idx ON users (storage_dir);

ALTER TABLE documents ADD COLUMN storage_owner TEXT;

UPDATE documents d SET storage_owner = (
    SELECT u.storage_dir
    FROM users u
    WHERE u.id = COALESCE(
        (SELECT uc.user_id
         FROM (SELECT cause_id FROM source_causes WHERE document_id = d.id
               UNION
               SELECT cs.cause_id FROM source_cases sc JOIN cases cs ON cs.id = sc.case_id
               WHERE sc.document_id = d.id) linked
         JOIN causes c ON c.id = linked.cause_id
         JOIN user_causes uc ON uc.cause_id = c.id AND uc.role = 'owner'
         ORDER BY c.created_at, c.id, uc.created_at, uc.user_id LIMIT 1),
        (SELECT us.user_id FROM user_sources us WHERE us.document_id = d.id
         ORDER BY (us.role = 'owner') DESC, us.created_at, us.user_id LIMIT 1))
);

COMMIT;
