-- User emails and full names are stored encrypted (encryptor.py). Emails are
-- looked up (sign-in, sign-up duplicates, invitations) through email_hash, a
-- keyed hash of the lowercased address, instead of lower(email). Existing
-- rows are converted by db/encrypt_users.py, which also drops the old
-- lower(email) index (kept until then so duplicates stay impossible);
-- deploy/remote-deploy.sh runs it straight after this. Apply with:
--
--   psql case_manager -f db/migrations/042_encrypt_user_identity.sql
--   python db/encrypt_users.py

BEGIN;

ALTER TABLE users ADD COLUMN email_hash TEXT;
CREATE UNIQUE INDEX users_email_hash_idx ON users (email_hash);

COMMIT;
