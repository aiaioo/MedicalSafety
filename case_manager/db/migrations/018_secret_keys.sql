-- Sharing by secret key. The owner of a cause, case, allegation, report or
-- document can create keys for it (each "viewer" or "editor"); anyone who
-- opens that object's URL without being signed in is asked for a key and a
-- captcha, and on success gets a guest session with the key's permission.
--
--   psql case_manager -f db/migrations/018_secret_keys.sql
--
-- A key is 32 random characters. Turning its "active" flag off suspends it
-- without deleting it (visitors are told it was temporarily deactivated);
-- deleting the row unshares the object. Redeeming a key adds a row to
-- key_redemptions for the (guest) user; the eff_user_* views below turn the
-- redemptions of *active* keys into viewer/editor access, which then cascades
-- down the hierarchy exactly like a role granted on the object itself (see
-- 016_inherited_access.sql). Deleting or deactivating a key therefore cuts
-- off everyone who used it at once.

BEGIN;

-- Guests: throwaway users created when someone unlocks a key without an account.
ALTER TABLE users ADD COLUMN is_guest BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE cause_keys (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cause_id TEXT NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    key         TEXT NOT NULL UNIQUE CHECK (char_length(key) = 32),
    permission  TEXT NOT NULL CHECK (permission IN ('editor', 'viewer')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX cause_keys_cause_id_idx ON cause_keys (cause_id);
CREATE INDEX cause_keys_owner_id_idx ON cause_keys (owner_id);

CREATE TABLE case_keys (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    key         TEXT NOT NULL UNIQUE CHECK (char_length(key) = 32),
    permission  TEXT NOT NULL CHECK (permission IN ('editor', 'viewer')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX case_keys_case_id_idx ON case_keys (case_id);
CREATE INDEX case_keys_owner_id_idx ON case_keys (owner_id);

CREATE TABLE allegation_keys (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    allegation_id TEXT NOT NULL REFERENCES allegations(id) ON DELETE CASCADE,
    key         TEXT NOT NULL UNIQUE CHECK (char_length(key) = 32),
    permission  TEXT NOT NULL CHECK (permission IN ('editor', 'viewer')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX allegation_keys_allegation_id_idx ON allegation_keys (allegation_id);
CREATE INDEX allegation_keys_owner_id_idx ON allegation_keys (owner_id);

CREATE TABLE report_keys (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    key         TEXT NOT NULL UNIQUE CHECK (char_length(key) = 32),
    permission  TEXT NOT NULL CHECK (permission IN ('editor', 'viewer')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX report_keys_report_id_idx ON report_keys (report_id);
CREATE INDEX report_keys_owner_id_idx ON report_keys (owner_id);

CREATE TABLE document_keys (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    key         TEXT NOT NULL UNIQUE CHECK (char_length(key) = 32),
    permission  TEXT NOT NULL CHECK (permission IN ('editor', 'viewer')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX document_keys_document_id_idx ON document_keys (document_id);
CREATE INDEX document_keys_owner_id_idx ON document_keys (owner_id);

-- Keys a user has entered. `kind` says which key table `key` is looked up in.
CREATE TABLE key_redemptions (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('cause', 'case', 'allegation', 'report', 'source')),
    key         TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind, key)
);

-- The redemptions of active keys of one kind, as (user_id, object id, rank).

CREATE OR REPLACE VIEW eff_user_causes AS
SELECT user_id, cause_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, cause_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_causes
    UNION ALL
    SELECT r.user_id, k.cause_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
        FROM key_redemptions r JOIN cause_keys k ON k.key = r.key AND k.active WHERE r.kind = 'cause'
) t GROUP BY user_id, cause_id;

CREATE OR REPLACE VIEW eff_user_cases AS
SELECT user_id, case_id,
       CASE max(rk) WHEN 3 THEN 'owner' WHEN 2 THEN 'editor' ELSE 'viewer' END AS role
FROM (
    SELECT user_id, case_id, CASE role WHEN 'owner' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END AS rk FROM user_cases
    UNION ALL
    SELECT r.user_id, k.case_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
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
    SELECT r.user_id, k.allegation_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
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
    SELECT r.user_id, k.report_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
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
    SELECT r.user_id, k.document_id, CASE k.permission WHEN 'editor' THEN 2 ELSE 1 END
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
