-- Every user has a default cause. Apply after 002_allegation_cause.sql:
--
--   psql case_manager -f db/migrations/003_default_cause_for_every_user.sql
--
-- A user with none gets the cause they can edit that was updated most
-- recently; a user who can edit no cause gets a new "General" one they own
-- (new sign-ups get one the same way, in auth.py's User.register).

BEGIN;

UPDATE users u SET default_cause_id = (
    SELECT c.id FROM causes c
    JOIN user_causes uc ON uc.cause_id = c.id AND uc.user_id = u.id AND uc.role IN ('owner', 'editor')
    ORDER BY c.updated_at DESC, c.id LIMIT 1
)
WHERE u.default_cause_id IS NULL;

DO $$
DECLARE
    u   RECORD;
    cid TEXT;
BEGIN
    FOR u IN SELECT id FROM users WHERE default_cause_id IS NULL LOOP
        cid := 'general-' || substr(md5(random()::text || u.id::text), 1, 6);
        INSERT INTO causes (id, title, description) VALUES (cid, 'General', '');
        INSERT INTO user_causes (user_id, cause_id, role) VALUES (u.id, cid, 'owner');
        UPDATE users SET default_cause_id = cid WHERE id = u.id;
    END LOOP;
END $$;

COMMIT;
