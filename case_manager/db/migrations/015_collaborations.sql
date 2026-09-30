-- Collaborations: a user invites another (by email) to collaborate; once the
-- invitee accepts, the inviter can share their causes, cases, allegations,
-- reports and documents with them by adding rows to the user_* access tables.
-- Adds per-allegation access (user_allegations, like user_causes etc.) and
-- the collaborations table.
--
--   psql case_manager -f db/migrations/015_collaborations.sql
--
-- Deleting a user, cause, case, allegation, report or document removes the
-- matching access rows (ON DELETE CASCADE), so access never outlives its object.

BEGIN;

CREATE TABLE user_allegations (
    user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    allegation_id  TEXT NOT NULL REFERENCES allegations(id) ON DELETE CASCADE,
    role           TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, allegation_id)
);
CREATE INDEX user_allegations_allegation_id_idx ON user_allegations (allegation_id);

-- Until now an allegation was governed only by the role on its cause. Give
-- every user who has a role on an allegation's cause the same role on the
-- allegation itself, so existing access carries over.
INSERT INTO user_allegations (user_id, allegation_id, role)
    SELECT uc.user_id, a.id, uc.role FROM allegations a JOIN user_causes uc ON uc.cause_id = a.cause_id;

CREATE TABLE collaborations (
    id             BIGSERIAL PRIMARY KEY,
    inviter_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invitee_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    responded_at   TIMESTAMPTZ,
    -- Whether the inviter has yet seen that the invitation was accepted
    -- (drives the title-bar notification).
    inviter_seen   BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (inviter_id <> invitee_id)
);
-- At most one collaboration between two users, whichever of them invited.
CREATE UNIQUE INDEX collaborations_pair_idx ON collaborations (LEAST(inviter_id, invitee_id), GREATEST(inviter_id, invitee_id));
CREATE INDEX collaborations_invitee_idx ON collaborations (invitee_id);
CREATE INDEX collaborations_inviter_idx ON collaborations (inviter_id);

COMMIT;
