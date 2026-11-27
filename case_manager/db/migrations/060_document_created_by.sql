-- Who created (uploaded) each document. The document may belong to someone
-- else: an upload into a collaborator's workspace is owned by the
-- collaborator. Its creator may delete it, as its owner may. NULL if unknown,
-- or if the creator's account was deleted.
--
-- Existing documents are filled in from the access granted at upload, which was
-- written in the same transaction as the document and so carries the same
-- timestamp: the uploader is the non-owner granted then (an upload into
-- someone else's workspace, or a document whose ownership was later handed
-- over), else the owner granted then. The parts of a split document copied
-- the original's grants, so for them the earliest grants are read the same way.
-- Where an uploader's access has since been removed, this names the owner.
--
--   psql case_manager -f db/migrations/060_document_created_by.sql

BEGIN;

ALTER TABLE documents ADD COLUMN created_by BIGINT REFERENCES users(id) ON DELETE SET NULL;

UPDATE documents d SET created_by = (
    SELECT us.user_id FROM user_sources us
    WHERE us.document_id = d.id AND us.created_at = d.created_at
    ORDER BY (us.role = 'owner'), us.user_id LIMIT 1);

UPDATE documents d SET created_by = (
    SELECT us.user_id FROM user_sources us
    WHERE us.document_id = d.id
      AND us.created_at = (SELECT min(created_at) FROM user_sources WHERE document_id = d.id)
    ORDER BY (us.role = 'owner'), us.user_id LIMIT 1)
WHERE d.created_by IS NULL
  AND EXISTS (SELECT 1 FROM document_splits s, jsonb_array_elements(s.plan -> 'segments') AS seg
              WHERE s.status IN ('committed', 'done') AND seg ->> 'id' = d.id);

COMMIT;
