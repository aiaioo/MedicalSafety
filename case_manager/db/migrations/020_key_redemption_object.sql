-- Remember which object a redeemed key opened, so that when the owner later
-- deletes the key its holder can be told "your key was deleted" rather than
-- that the object doesn't exist. Deleting a key no longer forgets its
-- redemptions (the eff_user_* views only count redemptions whose key still
-- exists, so access is cut off all the same); they are swept away with the
-- key session.
--
--   psql case_manager -f db/migrations/020_key_redemption_object.sql

BEGIN;

ALTER TABLE key_redemptions ADD COLUMN object_id TEXT;

UPDATE key_redemptions r SET object_id = k.cause_id FROM cause_keys k WHERE r.kind = 'cause' AND k.key = r.key;
UPDATE key_redemptions r SET object_id = k.case_id FROM case_keys k WHERE r.kind = 'case' AND k.key = r.key;
UPDATE key_redemptions r SET object_id = k.allegation_id FROM allegation_keys k WHERE r.kind = 'allegation' AND k.key = r.key;
UPDATE key_redemptions r SET object_id = k.report_id FROM report_keys k WHERE r.kind = 'report' AND k.key = r.key;
UPDATE key_redemptions r SET object_id = k.document_id FROM document_keys k WHERE r.kind = 'source' AND k.key = r.key;

COMMIT;
