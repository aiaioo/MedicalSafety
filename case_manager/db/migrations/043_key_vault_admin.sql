-- The key vault admin: the one login that can enter the encryption key (see
-- key_gate.py). Not an app user and unrelated to users.is_admin -- it has to
-- work before the key is loaded, when no user can sign in. The login name is
-- fixed ('admin'); until the password is changed here, the default one built
-- into key_gate.py applies. At most one row. Apply with:
--
--   psql case_manager -f db/migrations/043_key_vault_admin.sql

BEGIN;

CREATE TABLE key_vault_admin (
    id            SMALLINT PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
