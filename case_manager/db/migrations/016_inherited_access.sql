-- Access cascades down the hierarchy
--
--     causes -> cases -> allegations -> reports -> sources
--
-- A role on a node applies to everything beneath it (never higher than
-- "editor" -- ownership, and so deleting, is never inherited), and a user
-- can be given a stronger role on a particular descendant. Their effective
-- role on an object is the highest of the role granted on the object itself
-- and the roles inherited from every parent.
--
-- The user_* tables still hold only the roles granted directly. These views
-- add what is inherited; the application reads access through the views and
-- writes to the tables. Because they are computed, children added later are
-- covered too, and revoking a parent's role removes the inherited access.
--
-- Parent -> child links used:
--   cause -> case         cases.cause_id
--   cause -> allegation   allegations.cause_id
--   cause -> report       report_causes
--   cause -> source       source_causes
--   case  -> allegation   allegation_cases
--   case  -> report       report_cases
--   case  -> source       source_cases
--   allegation -> report  allegation_evidence.report_id
--   report -> source      reports.source_document_id, and the documents of the
--                         report's snippets (report_snippets -> snippets)
--
--   psql case_manager -f db/migrations/016_inherited_access.sql

BEGIN;

CREATE VIEW eff_user_causes AS
    SELECT user_id, cause_id, role FROM user_causes;

CREATE VIEW eff_user_cases AS
SELECT user_id, case_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, case_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_cases
    UNION ALL
    SELECT e.user_id, c.id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_causes e JOIN cases c ON c.cause_id = e.cause_id
) t GROUP BY user_id, case_id;

CREATE VIEW eff_user_allegations AS
SELECT user_id, allegation_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, allegation_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_allegations
    UNION ALL
    SELECT e.user_id, a.id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_causes e JOIN allegations a ON a.cause_id = e.cause_id
    UNION ALL
    SELECT e.user_id, ac.allegation_id, CASE e.role WHEN 'viewer' THEN 1 ELSE 2 END
        FROM eff_user_cases e JOIN allegation_cases ac ON ac.case_id = e.case_id
) t GROUP BY user_id, allegation_id;

CREATE VIEW eff_user_reports AS
SELECT user_id, report_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, report_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_reports
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

CREATE VIEW eff_user_sources AS
SELECT user_id, document_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, document_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_sources
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
