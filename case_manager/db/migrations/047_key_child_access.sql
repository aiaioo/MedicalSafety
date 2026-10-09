-- Edit access for particular reports beneath a Share link's object. A link
-- has one permission for its object and everything beneath it (see
-- 046_creator_role.sql); the owner can now also name individual reports under
-- it that the link lets its holders edit -- e.g. a view-only link to a cause
-- that lets the holder edit a few of its reports. The documents a report draws
-- on come with it (as they do for any editor of a report), and no other
-- documents do.
--
-- The grants hang off the key's string (unique per key), so deleting the key
-- removes them (the app does that), and switching it off suspends them.
--
--   psql case_manager -f db/migrations/047_key_child_access.sql

BEGIN;

CREATE TABLE key_child_access (
    key        TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind = 'report'),
    object_id  TEXT NOT NULL,
    PRIMARY KEY (key, kind, object_id)
);

CREATE VIEW active_keys AS
    SELECT key FROM cause_keys WHERE active
    UNION ALL SELECT key FROM case_keys WHERE active
    UNION ALL SELECT key FROM allegation_keys WHERE active
    UNION ALL SELECT key FROM report_keys WHERE active
    UNION ALL SELECT key FROM document_keys WHERE active;

CREATE OR REPLACE VIEW eff_user_reports AS
SELECT user_id, report_id,
       CASE max(rk) WHEN 4 THEN 'owner' WHEN 3 THEN 'editor' WHEN 2 THEN 'creator' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, report_id, CASE role WHEN 'owner' THEN 4 WHEN 'editor' THEN 3 WHEN 'creator' THEN 2 ELSE 1 END AS rk FROM user_reports
    UNION ALL
    SELECT -r.session_id, k.report_id, CASE k.permission WHEN 'editor' THEN 3 ELSE 1 END
        FROM key_redemptions r JOIN report_keys k ON k.key = r.key AND k.active WHERE r.kind = 'report'
    UNION ALL
    SELECT -r.session_id, g.object_id, 3
        FROM key_redemptions r JOIN key_child_access g ON g.key = r.key AND g.kind = 'report'
        JOIN active_keys a ON a.key = r.key
    UNION ALL
    SELECT e.user_id, rc.report_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_causes e JOIN report_causes rc ON rc.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, rc.report_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_cases e JOIN report_cases rc ON rc.case_id = e.case_id
    UNION ALL
    SELECT e.user_id, ae.report_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_allegations e JOIN allegation_evidence ae ON ae.allegation_id = e.allegation_id
        WHERE ae.report_id IS NOT NULL
) t GROUP BY user_id, report_id;

COMMIT;
