-- Key holders without an account. Replaces the throwaway guest users of
-- 018_secret_keys.sql: someone who enters a key now gets a row in
-- key_sessions (no users row, no sign-in), and the keys they have entered are
-- recorded against that session in key_redemptions.
--
--   psql case_manager -f db/migrations/019_key_sessions.sql
--
-- The eff_user_* views identify a viewer by a user id. A key session is
-- presented to them as the *negative* of its id, which can never collide with
-- a real user's (positive, BIGSERIAL) id -- so every existing access check
-- works unchanged, and a key session's access is exactly the viewer/editor
-- permission of the active keys it has redeemed, cascaded to child objects.

BEGIN;

DELETE FROM users WHERE is_guest;
ALTER TABLE users DROP COLUMN is_guest;

CREATE TABLE key_sessions (
    id          BIGSERIAL PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
-- Redemptions were per guest user; make them per key session. (The views
-- below depend on this table, so it is altered in place rather than recreated.)
DELETE FROM key_redemptions;
ALTER TABLE key_redemptions DROP CONSTRAINT key_redemptions_user_id_fkey;
ALTER TABLE key_redemptions RENAME COLUMN user_id TO session_id;
ALTER TABLE key_redemptions
    ADD FOREIGN KEY (session_id) REFERENCES key_sessions(id) ON DELETE CASCADE;

CREATE OR REPLACE VIEW eff_user_causes AS
SELECT user_id, cause_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, cause_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_causes
    UNION ALL
    SELECT -r.session_id, k.cause_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
        FROM key_redemptions r JOIN cause_keys k ON k.key = r.key AND k.active WHERE r.kind = 'cause'
) t GROUP BY user_id, cause_id;

CREATE OR REPLACE VIEW eff_user_cases AS
SELECT user_id, case_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, case_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_cases
    UNION ALL
    SELECT -r.session_id, k.case_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
        FROM key_redemptions r JOIN case_keys k ON k.key = r.key AND k.active WHERE r.kind = 'case'
    UNION ALL
    SELECT e.user_id, c.id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_causes e JOIN cases c ON c.cause_id = e.cause_id
) t GROUP BY user_id, case_id;

CREATE OR REPLACE VIEW eff_user_allegations AS
SELECT user_id, allegation_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, allegation_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_allegations
    UNION ALL
    SELECT -r.session_id, k.allegation_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
        FROM key_redemptions r JOIN allegation_keys k ON k.key = r.key AND k.active WHERE r.kind = 'allegation'
    UNION ALL
    SELECT e.user_id, a.id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_causes e JOIN allegations a ON a.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, ac.allegation_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_cases e JOIN allegation_cases ac ON ac.case_id = e.case_id
) t GROUP BY user_id, allegation_id;

CREATE OR REPLACE VIEW eff_user_reports AS
SELECT user_id, report_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, report_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_reports
    UNION ALL
    SELECT -r.session_id, k.report_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
        FROM key_redemptions r JOIN report_keys k ON k.key = r.key AND k.active WHERE r.kind = 'report'
    UNION ALL
    SELECT e.user_id, rc.report_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_causes e JOIN report_causes rc ON rc.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, rc.report_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_cases e JOIN report_cases rc ON rc.case_id = e.case_id
    UNION ALL
    SELECT e.user_id, ae.report_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_allegations e JOIN allegation_evidence ae ON ae.allegation_id = e.allegation_id
        WHERE ae.report_id IS NOT NULL
) t GROUP BY user_id, report_id;

CREATE OR REPLACE VIEW eff_user_sources AS
SELECT user_id, document_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, document_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_sources
    UNION ALL
    SELECT -r.session_id, k.document_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
        FROM key_redemptions r JOIN document_keys k ON k.key = r.key AND k.active WHERE r.kind = 'source'
    UNION ALL
    SELECT e.user_id, sc.document_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_causes e JOIN source_causes sc ON sc.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, sc.document_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_cases e JOIN source_cases sc ON sc.case_id = e.case_id
    UNION ALL
    SELECT e.user_id, r.source_document_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_reports e JOIN reports r ON r.id = e.report_id
        WHERE r.source_document_id IS NOT NULL
    UNION ALL
    SELECT e.user_id, s.document_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_reports e JOIN report_snippets rs ON rs.report_id = e.report_id
        JOIN snippets s ON s.id = rs.snippet_id
) t GROUP BY user_id, document_id;


COMMIT;
