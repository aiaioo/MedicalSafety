-- "View and create": a role between viewer and editor. On a cause it lets the
-- holder view everything under it and create things beneath it -- cases,
-- allegations, reports and uploaded documents -- and, as the creator, edit
-- (and own) whatever they create. It does not let them change what others
-- made, nor the cause itself. Ranks are now
--
--     viewer 1 < creator 2 < editor 3 < owner 4
--
-- A creator role is only ever granted on a cause (a share to a collaborator);
-- on everything beneath the cause it counts as viewer, so the things a
-- creator makes themselves are what they can edit. Editors and owners can
-- create too, as before.
--
-- Share links (secret keys) never create: anyone may hold a link, so a link
-- can only let its holder view -- except a report's or document's link, which
-- may allow editing (a report's editor access reaches its documents). Keys on
-- causes, cases and allegations that allowed editing are reduced to viewing.
--
-- Apply after 045_cause_activity.sql:
--
--   psql case_manager -f db/migrations/046_creator_role.sql

BEGIN;

ALTER TABLE user_causes DROP CONSTRAINT user_causes_role_check;
ALTER TABLE user_causes ADD CONSTRAINT user_causes_role_check CHECK (role IN ('owner', 'editor', 'creator', 'viewer'));

UPDATE cause_keys SET permission = 'viewer' WHERE permission <> 'viewer';
UPDATE case_keys SET permission = 'viewer' WHERE permission <> 'viewer';
UPDATE allegation_keys SET permission = 'viewer' WHERE permission <> 'viewer';
ALTER TABLE cause_keys DROP CONSTRAINT cause_keys_permission_check;
ALTER TABLE cause_keys ADD CONSTRAINT cause_keys_permission_check CHECK (permission = 'viewer');
ALTER TABLE case_keys DROP CONSTRAINT case_keys_permission_check;
ALTER TABLE case_keys ADD CONSTRAINT case_keys_permission_check CHECK (permission = 'viewer');
ALTER TABLE allegation_keys DROP CONSTRAINT allegation_keys_permission_check;
ALTER TABLE allegation_keys ADD CONSTRAINT allegation_keys_permission_check CHECK (permission = 'viewer');

-- A role granted directly -> its rank.
-- A role on a parent -> the rank it gives a child: viewer and creator both
-- give viewer; editor and owner give editor (ownership is never inherited).

CREATE OR REPLACE VIEW eff_user_causes AS
SELECT user_id, cause_id,
       CASE max(rk) WHEN 4 THEN 'owner' WHEN 3 THEN 'editor' WHEN 2 THEN 'creator' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, cause_id, CASE role WHEN 'owner' THEN 4 WHEN 'editor' THEN 3 WHEN 'creator' THEN 2 ELSE 1 END AS rk FROM user_causes
    UNION ALL
    SELECT -r.session_id, k.cause_id, 1
        FROM key_redemptions r JOIN cause_keys k ON k.key = r.key AND k.active WHERE r.kind = 'cause'
) t GROUP BY user_id, cause_id;

CREATE OR REPLACE VIEW eff_user_cases AS
SELECT user_id, case_id,
       CASE max(rk) WHEN 4 THEN 'owner' WHEN 3 THEN 'editor' WHEN 2 THEN 'creator' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, case_id, CASE role WHEN 'owner' THEN 4 WHEN 'editor' THEN 3 WHEN 'creator' THEN 2 ELSE 1 END AS rk FROM user_cases
    UNION ALL
    SELECT -r.session_id, k.case_id, 1
        FROM key_redemptions r JOIN case_keys k ON k.key = r.key AND k.active WHERE r.kind = 'case'
    UNION ALL
    SELECT e.user_id, c.id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_causes e JOIN cases c ON c.cause_id = e.cause_id
) t GROUP BY user_id, case_id;

CREATE OR REPLACE VIEW eff_user_allegations AS
SELECT user_id, allegation_id,
       CASE max(rk) WHEN 4 THEN 'owner' WHEN 3 THEN 'editor' WHEN 2 THEN 'creator' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, allegation_id, CASE role WHEN 'owner' THEN 4 WHEN 'editor' THEN 3 WHEN 'creator' THEN 2 ELSE 1 END AS rk FROM user_allegations
    UNION ALL
    SELECT -r.session_id, k.allegation_id, 1
        FROM key_redemptions r JOIN allegation_keys k ON k.key = r.key AND k.active WHERE r.kind = 'allegation'
    UNION ALL
    SELECT e.user_id, a.id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_causes e JOIN allegations a ON a.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, ac.allegation_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_cases e JOIN allegation_cases ac ON ac.case_id = e.case_id
) t GROUP BY user_id, allegation_id;

CREATE OR REPLACE VIEW eff_user_reports AS
SELECT user_id, report_id,
       CASE max(rk) WHEN 4 THEN 'owner' WHEN 3 THEN 'editor' WHEN 2 THEN 'creator' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, report_id, CASE role WHEN 'owner' THEN 4 WHEN 'editor' THEN 3 WHEN 'creator' THEN 2 ELSE 1 END AS rk FROM user_reports
    UNION ALL
    SELECT -r.session_id, k.report_id, CASE k.permission WHEN 'editor' THEN 3 ELSE 1 END
        FROM key_redemptions r JOIN report_keys k ON k.key = r.key AND k.active WHERE r.kind = 'report'
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

CREATE OR REPLACE VIEW eff_user_sources AS
SELECT user_id, document_id,
       CASE max(rk) WHEN 4 THEN 'owner' WHEN 3 THEN 'editor' WHEN 2 THEN 'creator' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, document_id, CASE role WHEN 'owner' THEN 4 WHEN 'editor' THEN 3 WHEN 'creator' THEN 2 ELSE 1 END AS rk FROM user_sources
    UNION ALL
    SELECT -r.session_id, k.document_id, CASE k.permission WHEN 'editor' THEN 3 ELSE 1 END
        FROM key_redemptions r JOIN document_keys k ON k.key = r.key AND k.active WHERE r.kind = 'source'
    UNION ALL
    SELECT e.user_id, sc.document_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_causes e JOIN source_causes sc ON sc.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, sc.document_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_cases e JOIN source_cases sc ON sc.case_id = e.case_id
    UNION ALL
    SELECT e.user_id, r.source_document_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_reports e JOIN reports r ON r.id = e.report_id
        WHERE r.source_document_id IS NOT NULL
    UNION ALL
    SELECT e.user_id, s.document_id, CASE WHEN e.role IN ('viewer', 'creator') THEN 1 ELSE 3 END
        FROM eff_user_reports e JOIN report_snippets rs ON rs.report_id = e.report_id
        JOIN snippets s ON s.id = rs.snippet_id
) t GROUP BY user_id, document_id;

COMMIT;
