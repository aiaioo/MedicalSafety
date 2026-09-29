-- ---------------------------------------------------------------------------
-- Users, sign-in sessions, and per-user access to causes / cases / reports /
-- sources. Apply after db/schema.sql (and after db/migrate_json_to_postgres.py,
-- if you're importing an old storage/*.json tree -- the final step below makes
-- the seed user the owner of every row that already exists, so nothing
-- imported becomes invisible):
--
--   psql case_manager -f db/migrations/001_users_and_access.sql
--
-- Access model: a user can see or touch a cause/case/report/source only
-- through a row in the matching user_* association table below, and only as
-- far as that row's role allows:
--   viewer -- read it
--   editor -- read and change it; for a cause, also create cases under it;
--             for a cause or case, also create reports / upload sources
--             associated with it (see the association tables below)
--   owner  -- everything an editor can, plus delete it. Whoever creates an
--             object becomes its owner.
-- Access is per object, not inherited: being an editor of a cause doesn't by
-- itself let someone see that cause's cases.
-- ---------------------------------------------------------------------------

BEGIN;

CREATE TABLE users (
    id                  BIGSERIAL PRIMARY KEY,
    email               TEXT NOT NULL,
    -- Never the password itself: a salted, deliberately slow one-way hash in
    -- werkzeug.security's self-describing "method$salt$hash" format (scrypt
    -- by default), so the hashing parameters can be strengthened later
    -- without a schema change -- see auth.py's User class.
    password_hash       TEXT NOT NULL,
    -- The user's default cause: the cause they last selected (in the causes
    -- workspace, or for a case). Every report/source they create is
    -- associated with it, and a new case is put under it when no cause is
    -- given. Replaces the global app_settings.last_used_cause_id.
    default_cause_id    TEXT REFERENCES causes(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Emails are stored lowercased by the app, but enforce case-insensitive
-- uniqueness here too so two accounts can never differ only by case.
CREATE UNIQUE INDEX users_email_lower_idx ON users (lower(email));

-- One row per signed-in browser. The cookie carries a random token; only its
-- SHA-256 is stored, so a leaked copy of this table can't be replayed as a
-- live session.
CREATE TABLE user_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX user_sessions_user_id_idx ON user_sessions (user_id);

CREATE TABLE user_causes (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cause_id    TEXT NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, cause_id)
);
CREATE INDEX user_causes_cause_id_idx ON user_causes (cause_id);

CREATE TABLE user_cases (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, case_id)
);
CREATE INDEX user_cases_case_id_idx ON user_cases (case_id);

CREATE TABLE user_reports (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id   TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, report_id)
);
CREATE INDEX user_reports_report_id_idx ON user_reports (report_id);

-- "Sources" are the uploaded documents (the documents table); access to one
-- also covers its annotations and snippets.
CREATE TABLE user_sources (
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, document_id)
);
CREATE INDEX user_sources_document_id_idx ON user_sources (document_id);

-- Reports and sources don't belong to a cause or case; they're associated
-- with any number of causes and/or cases (many-to-many). A newly created
-- report or uploaded source is associated with its creator's default cause
-- (users.default_cause_id); more associations can be added afterwards. Deleting
-- either side just removes the association.
CREATE TABLE report_causes (
    report_id   TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    cause_id    TEXT NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, cause_id)
);
CREATE INDEX report_causes_cause_id_idx ON report_causes (cause_id);

CREATE TABLE report_cases (
    report_id   TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, case_id)
);
CREATE INDEX report_cases_case_id_idx ON report_cases (case_id);

CREATE TABLE source_causes (
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    cause_id     TEXT NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, cause_id)
);
CREATE INDEX source_causes_cause_id_idx ON source_causes (cause_id);

CREATE TABLE source_cases (
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    case_id      TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, case_id)
);
CREATE INDEX source_cases_case_id_idx ON source_cases (case_id);

-- Seed account. The hash is werkzeug's scrypt hash of the initial password
-- (see users.password_hash above) -- change that password after first
-- sign-in on any deployment reachable by anyone else.
INSERT INTO users (email, password_hash, default_cause_id)
VALUES (
    'cohan.sujay@gmail.com',
    'scrypt:32768:8:1$dZ5hOMrBreI8aNhk$c12d813274c12ae18df16d515ee538519a86244065d9cc30336f0954cbe8b00a58dbcf51927377c47b1c6c078059a4ac8fde9da8973951713341fecb985827ed',
    (SELECT last_used_cause_id FROM app_settings WHERE id = TRUE)
);

-- Everything that existed before users did is owned by the seed account, so
-- it stays reachable after this migration.
INSERT INTO user_causes (user_id, cause_id, role)
    SELECT u.id, c.id, 'owner' FROM users u CROSS JOIN causes c WHERE u.email = 'cohan.sujay@gmail.com';
INSERT INTO user_cases (user_id, case_id, role)
    SELECT u.id, c.id, 'owner' FROM users u CROSS JOIN cases c WHERE u.email = 'cohan.sujay@gmail.com';
INSERT INTO user_reports (user_id, report_id, role)
    SELECT u.id, r.id, 'owner' FROM users u CROSS JOIN reports r WHERE u.email = 'cohan.sujay@gmail.com';
INSERT INTO user_sources (user_id, document_id, role)
    SELECT u.id, d.id, 'owner' FROM users u CROSS JOIN documents d WHERE u.email = 'cohan.sujay@gmail.com';

COMMIT;
