-- Every report and uploaded document is associated with a cause (directly,
-- or through a case). Older ones created before that rule are associated
-- with nothing; put each under a "General" cause owned by its owner (the
-- earliest-created one they own, or a new one if they have none). Apply
-- after 005_signup_attempts.sql:
--
--   psql case_manager -f db/migrations/006_general_cause_for_unassociated.sql
--
-- An item with no owner uses the earliest-created user with any role on it;
-- one with no user at all is left alone.

BEGIN;

CREATE TEMP TABLE unassociated (kind TEXT, item_id TEXT, user_id BIGINT) ON COMMIT DROP;

INSERT INTO unassociated
SELECT 'report', r.id, (
    SELECT ur.user_id FROM user_reports ur WHERE ur.report_id = r.id
    ORDER BY (ur.role = 'owner') DESC, ur.created_at, ur.user_id LIMIT 1)
FROM reports r
WHERE NOT EXISTS (SELECT 1 FROM report_causes x WHERE x.report_id = r.id)
  AND NOT EXISTS (SELECT 1 FROM report_cases x WHERE x.report_id = r.id);

INSERT INTO unassociated
SELECT 'source', d.id, (
    SELECT us.user_id FROM user_sources us WHERE us.document_id = d.id
    ORDER BY (us.role = 'owner') DESC, us.created_at, us.user_id LIMIT 1)
FROM documents d
WHERE NOT EXISTS (SELECT 1 FROM source_causes x WHERE x.document_id = d.id)
  AND NOT EXISTS (SELECT 1 FROM source_cases x WHERE x.document_id = d.id);

DELETE FROM unassociated WHERE user_id IS NULL;

DO $$
DECLARE
    u   RECORD;
    cid TEXT;
BEGIN
    FOR u IN SELECT DISTINCT user_id FROM unassociated LOOP
        SELECT c.id INTO cid FROM causes c
        JOIN user_causes uc ON uc.cause_id = c.id AND uc.user_id = u.user_id AND uc.role = 'owner'
        WHERE c.title = 'General' ORDER BY c.created_at, c.id LIMIT 1;
        IF cid IS NULL THEN
            cid := 'general-' || substr(md5(random()::text || u.user_id::text), 1, 6);
            INSERT INTO causes (id, title, description) VALUES (cid, 'General', '');
            INSERT INTO user_causes (user_id, cause_id, role) VALUES (u.user_id, cid, 'owner');
        END IF;
        INSERT INTO report_causes (report_id, cause_id)
            SELECT item_id, cid FROM unassociated WHERE user_id = u.user_id AND kind = 'report';
        INSERT INTO source_causes (document_id, cause_id)
            SELECT item_id, cid FROM unassociated WHERE user_id = u.user_id AND kind = 'source';
    END LOOP;
END $$;

COMMIT;
