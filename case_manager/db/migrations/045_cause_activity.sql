-- Activity log for each cause: downloads, the first view per day, and saves of
-- the cause itself or of anything under it (its cases, allegations, reports and
-- documents). Apply after 044_user_document_sections.sql:
--
--   psql case_manager -f db/migrations/045_cause_activity.sql
--
-- actor_id is a user id, or the negative of a key session id for a Share-link
-- visitor (the same convention as the eff_user_* views). It has no foreign key,
-- nor has resource_id, so the log outlives deleted users and resources. Names
-- are resolved when the log is read, because titles are stored encrypted.
-- `day` is the UTC date of occurred_at; the unique index below keeps a view to
-- one row per actor, resource and cause per day.

BEGIN;

CREATE TABLE cause_activity (
    id             BIGSERIAL PRIMARY KEY,
    cause_id       TEXT NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    day            DATE NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
    action         TEXT NOT NULL CHECK (action IN ('download', 'view', 'save')),
    resource_kind  TEXT NOT NULL CHECK (resource_kind IN ('cause', 'case', 'allegation', 'report', 'source')),
    resource_id    TEXT NOT NULL,
    actor_id       BIGINT NOT NULL,
    detail         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX cause_activity_cause_idx ON cause_activity (cause_id, day DESC, occurred_at DESC);
CREATE UNIQUE INDEX cause_activity_first_view_idx
    ON cause_activity (cause_id, actor_id, resource_kind, resource_id, day)
    WHERE action = 'view';

COMMIT;
