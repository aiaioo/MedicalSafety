# Key management and user-data encryption

How case_manager encrypts stored data, where the key lives, and what happens
when it is missing. Source of truth is the code: `key_manager.py`,
`encryptor.py`, `key_gate.py`, and the `_enc_text` / `_dec_text` helpers in
`storage.py`. Operational steps for the server are in `deploy/README.md`
("The encryption key").

## Summary

- One 32-byte (AES-256) key protects everything.
- The key is **never written to disk or the environment**. It lives only in
  the memory of the running server process and is typed in by an administrator
  after every start.
- Until it is entered the whole site is **locked**.
- Lose the key and every encrypted file and database value is unrecoverable.

## Modules

| File | Role |
| --- | --- |
| `key_manager.py` | Holds the key in memory (`KeyManager` singleton). Validates a candidate key against `key_check.enc`. Also the `generate` command and `prompt_for_key` for command-line scripts. |
| `encryptor.py` | Encrypts/decrypts bytes and files with the key. The only module meant to fetch the key (`key_manager.get_key`). Also `blind_index`. |
| `key_gate.py` | The lock: a before-request gate and the `/encryption-key` page where the key vault admin enters the key. |
| `storage.py` | `_enc_text` / `_dec_text` for encrypted database text columns; encrypts annotation shapes, report docs, report images, profile photos. |
| `key_check.enc` | Small committed file holding a known plaintext encrypted under the real key. Used to verify a typed key. |

## The key

- Generate: `python3 key_manager.py generate`. It prints a new random key once
  (URL-safe base64 of 32 bytes) and writes `key_check.enc` if that file does not
  exist. If `key_check.enc` already exists it is left unchanged and the new key
  will **not** match it (a warning is printed).
- Store a copy somewhere safe and offline. The server never persists it.
- Replacing the key is not supported at runtime: once a key is loaded,
  `/encryption-key` just reports done. Changing it means re-encrypting all data
  (see the `db/encrypt_*.py` conversion scripts for the pattern) and
  regenerating `key_check.enc`.

### Key check (`key_check.enc`)

A key is accepted only if it decrypts `key_check.enc` to exactly
`b"case_manager encryption key check v1"`. A malformed key, a wrong key, a
missing check file, or an altered check file raises `InvalidKey` and **nothing
is stored**. This stops a typo from being remembered and silently corrupting
new writes.

### In-memory only; one process

- `KeyManager` is a process-wide singleton, key held in `_key`, guarded by a lock.
- A restart (including every deploy) loses the key and re-locks the site.
- With several worker processes only the one that received the key would have
  it, so the app runs as a **single process with threads**
  (`deploy/case-manager.service`; one gunicorn worker). The dev server must not
  use the reloader for this reason: a reload starts a new process that needs
  unlocking again (comment near the bottom of `app.py`).
- Command-line scripts (`db/encrypt_*.py`, `encryptor.py encrypt|cat`) call
  `prompt_for_key`: key from stdin (hidden if a terminal), or from the terminal
  (`"tty"`) when stdin carries data. Example:
  `python3 db/encrypt_users.py < keyfile`.

## Cryptography

### Files and blobs (`encryptor.py`)

- Format: `MAGIC (b"MSENC1") | 7-byte random nonce prefix | chunks`.
- Each chunk is AES-256-GCM over up to 64 KiB of plaintext (`CHUNK_SIZE`), with
  nonce = prefix || 4-byte chunk counter || 1-byte last-chunk flag.
- Every chunk is authenticated; the last-chunk flag means reordering, dropping
  or truncating chunks is detected. A wrong key or tampering raises
  `InvalidToken`.
- `iter_decrypted` yields each chunk after verifying it, but tampering later in
  the file only surfaces when the generator reaches it. `read_encrypted` is
  all-or-nothing.
- Files on disk get a `.enc` suffix (`report.pdf` -> `report.pdf.enc`), written
  atomically (temp file + rename). Plaintext is only ever in memory.
- API: `write_encrypted`, `read_encrypted`, `open_encrypted` (seekable
  `BytesIO`, e.g. for `send_file` or PyMuPDF), `iter_decrypted`,
  `encrypt_bytes`, `decrypt_bytes`.

### Database text (`storage.py`)

- `_enc_text(value)` stores `"enc1:" + base64(encrypt_bytes(utf-8))`.
- `_dec_text(value)` reverses it. Values without the `enc1:` prefix are
  treated as not-yet-encrypted legacy text.

### Blind index (`encryptor.blind_index`)

For lookups by equality on encrypted values. Deterministic HMAC-SHA256 of the
text, using a sub-key derived from the master key
(`HMAC(key, "encryptor blind index v1")`). It **changes if the key changes**.

- `users.email_hash` = `blind_index(email.strip().lower())`; sign-in looks the
  user up by this hash because the email column itself is encrypted.
- Rate-limit keys (including client IPs) are also stored as blind indexes,
  since keys can embed email addresses.

## What is encrypted at rest

| Data | Where | How |
| --- | --- | --- |
| Uploaded documents (PDF) and all other stored files | storage backend (`EncryptedStorage`) | `.enc` files, AES-GCM |
| Document titles and descriptions | `documents` | `enc1:` text |
| Report names | `reports.name` | `enc1:` text |
| Report body | `reports.doc_enc` | encrypted JSON blob (thumbnail kept in its own column) |
| Report images | report image table | encrypted bytes |
| Annotation shapes | `document_annotations.shapes_enc` | encrypted JSON blob |
| Case, cause, hearing, party, goal and allegation text | respective tables | `enc1:` text |
| User email and full name | `users` | `enc1:` text, email also looked up via `email_hash` |
| Profile photos | `users` | encrypted bytes |
| Rate-limit keys / IPs | signup/unlock attempt tables | blind index |

Not encrypted (needs no key to read): credential hashes and structural columns
such as ids and timestamps.
Check `storage.py` for the authoritative list of encrypted columns.

Existing data was converted by one-off scripts in `db/` (`encrypt_existing_files.py`,
`encrypt_report_images.py`, `encrypt_annotations.py`, `encrypt_users.py`,
`encrypt_user_photos.py`, ...), with migrations such as
`040_encrypt_report_docs.sql`, `041_encrypt_annotations.sql`,
`042_encrypt_user_identity.sql`; `deploy/remote-deploy.sh` runs them.

## The lock (`key_gate.py`)

- `require_key_in_memory` is a `before_app_request` hook. While no key is in
  memory, every request except the open endpoints (`key_gate.encryption_key`,
  `key_gate.healthz`, `static`) is turned away:
  - pages: redirect to `/encryption-key`;
  - `/api/...`: HTTP 503 with a JSON error.
- It must be registered **before** `auth.bp`, because the auth gate reads
  encrypted columns (session user email).
- `GET /healthz` always answers and reports `{"ok": true, "key_loaded": bool}`.
  The deploy script uses it and prints that the site is locked until the key is
  entered.

### `KeyNotAvailable` handling and the redirect loop

- `app.py` has an error handler for `key_manager.KeyNotAvailable` as a
  backstop: pages redirect to `/encryption-key`, `/api/` returns 503. It
  re-raises when the request is for `/encryption-key` itself, so that page can
  never redirect to itself.
- `auth.load_user_and_require_signin` returns early when no key is loaded.
  Reason: the session lookup decrypts the user's email. Before this, a browser
  holding a session cookie made `/encryption-key` (exempt from the key gate but
  not from the auth hook) raise `KeyNotAvailable`, and the handler redirected
  to `/encryption-key` again, looping forever with a blank page.
- Lesson: anything that runs on the key-entry page must not touch encrypted data.

## The key vault admin and `/encryption-key`

- Entering the key is done by the **key vault admin**, who is *not* an app user
  and unrelated to `users.is_admin`. Accounts are looked up by an email hash
  that needs the key, so nobody can sign in before it is loaded.
- The credential is stored only as a hash, in its own table (migration 043),
  which needs no key to read. See `key_gate.py` for how it is set up and
  changed.
- The page has steps: `login` (credentials and captcha) -> `key` (enter the
  key) -> `done`.
- Protections: a captcha is checked first (and used up); repeated failures from
  an IP lock it out for a while (HTTP 429); the later steps need a signed,
  time-limited token produced by a successful login. The token signing secret
  and failure counters are per-process, which is fine because the app is a
  single process.
- The login page tells visitors: "The site is locked until the starting
  sequence is carried out.  Please inform the administrator."
  (`templates/encryption_key.html`).

## Operating it

1. Deploy (`deploy/deploy.sh`) -- the key is never part of a deploy. The
   service restarts locked.
2. Open `/encryption-key`, log in as the key vault admin, enter the key.
3. Verify with `/healthz` (`key_loaded: true`).
4. Any restart repeats steps 2-3. Back up the key offline.

Reminders recorded elsewhere: the droplet is a dev deployment; its blanket
sudo for the deploy user is temporary and should be restricted before
production.

## Failure modes

| Symptom | Cause |
| --- | --- |
| Every page redirects to `/encryption-key`, `/api/` returns 503 | Server restarted; key not entered yet. |
| "That key is wrong. Nothing was stored." | Key does not decrypt `key_check.enc`. |
| "The key check file is missing" | `key_check.enc` absent; no key can be verified. |
| `InvalidToken` reading a file or value | Wrong key, or ciphertext corrupted/tampered. |
| Email sign-in finds no user after a key change | `email_hash` is derived from the key; changing the key changes every hash. |
| Browser shows a blank page / many 302s on `/encryption-key` | The redirect loop described above (fixed in commit `3ff8f62`). |

## Design notes and limits

- Protects data at rest (disk, database dumps, backups). It does not protect
  against someone who can read the server process's memory or who gains admin
  control of the running, unlocked app.
- Plaintext is kept in memory only: uploads are not written to disk unencrypted.
- A single key encrypts everything; there is no per-user key and no key rotation
  yet.
