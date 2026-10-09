# Case Manager — document page viewer

A small Flask app that shows a single page of a document at a time, lets you
draw rectangle/freehand annotations on it, and lets you extract rectangular
snippets as PNG images. Every PDF page is rasterized on the server (via
PyMuPDF), so scanned/image-only PDFs, text PDFs, and Word documents are all
handled the same way in the browser.

## Setup

Dependencies (`Flask`, `PyMuPDF`) are already installed globally on this
machine. To run in a virtualenv instead:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

All structured data (cases, causes, allegations, reports, annotations,
snippet metadata) lives in PostgreSQL — see `db/schema.sql` and `storage.py`.
Create the database once and point the app at it:

```
createdb case_manager
psql case_manager -f db/schema.sql
psql case_manager -f db/migrations/001_users_and_access.sql
psql case_manager -f db/migrations/002_allegation_cause.sql
psql case_manager -f db/migrations/003_default_cause_for_every_user.sql
psql case_manager -f db/migrations/004_signup_captchas.sql
psql case_manager -f db/migrations/005_signup_attempts.sql
psql case_manager -f db/migrations/006_general_cause_for_unassociated.sql
psql case_manager -f db/migrations/007_report_snippets.sql
psql case_manager -f db/migrations/008_report_annexures.sql
psql case_manager -f db/migrations/009_annexure_all_pages_per_document.sql
psql case_manager -f db/migrations/010_annexure_page_mode.sql
export DATABASE_URL=postgresql:///case_manager   # defaults to this if unset
```

If you're migrating an existing `storage/*.json` tree from before this app
used a database, run `python3 db/migrate_json_to_postgres.py` once against
the new (empty) database to import it -- after `schema.sql` but *before*
the migrations in `db/migrations/`, so the imported rows get an owner.

## Users and access

Every page and API route requires signing in (`/signin`; new users register
at `/signup`). `db/migrations/001_users_and_access.sql` seeds one account,
`cohan.sujay@gmail.com`, and makes it the owner of everything that existed
before users did -- change its initial password on any shared deployment.
Passwords are stored only as salted scrypt hashes; sessions are server-side
rows keyed by the SHA-256 of a random, HttpOnly cookie token (see `auth.py`).

Causes, cases, reports and sources (uploaded documents, including their
annotations and snippets) are each visible only to users with a role on
that specific object, in the `user_causes` / `user_cases` / `user_reports` /
`user_sources` tables:

- **viewer** -- can read it.
- **creator** ("View and create"; granted on a cause only, as a share or a
  Share-link key) -- can also create things under the cause: cases,
  allegations, reports and uploaded documents. They own what they create, so
  they can edit it (and annotate their documents), but not what others made,
  nor the cause itself.
- **editor** -- can also change it. On a cause, an editor can also create
  cases and allegations under it. On a cause or case, an editor can also
  create reports and upload sources associated with it.
- **owner** -- can also delete it. Whoever creates something owns it.

Share links (secret keys) never let their holder create anything, and only
view -- except a report's or document's link, which may allow editing.
A view-only link on a cause, case or allegation can also be given edit access to
particular reports beneath it (`key_child_access`); the documents those reports
draw on become editable with them, and no others do.

Reports and sources don't belong to a cause or case. They're associated
with any number of causes and/or cases (`report_causes`, `report_cases`,
`source_causes`, `source_cases`). A new one is associated with its
creator's **default cause** (`users.default_cause_id`): the cause they last
selected in the causes workspace (or last put a case under). If that's
unset or they can no longer create in it, it's the most recently updated cause they can
create in (creator, editor or owner), or failing that a new "General" cause. Further associations are added or removed
with `POST` / `DELETE /api/report/<id>/links` or `/api/doc/<id>/links`
(body `{"cause_id": ...}` or `{"case_id": ...}`), which needs edit access to
both sides; the last association can't be removed.

Roles aren't inherited (a cause's editor doesn't automatically see its
cases), and there's no sharing UI yet: grant access with a row in the
matching table, e.g.
`INSERT INTO user_cases (user_id, case_id, role) VALUES (2, 'my-case-1a2b3c', 'viewer');`.
Allegations aren't per-user yet -- any signed-in user can see them.

Deploying to a server (e.g. a DigitalOcean droplet), including how
`DATABASE_URL` is kept as a secret rather than committed or hardcoded, is
covered in [`deploy/README.md`](deploy/README.md).

The document editor (`/document`) is a Tiptap-based rich text editor and needs
a one-time JS build (and again after editing anything under `static/src/`):

```
npm install
npm run build     # or `npm run watch` while developing
```

This bundles `static/src/editor.js` into `static/dist/editor.bundle.js`, which
`templates/document.html` loads — Flask itself serves it as a plain static
file, no Node process needed at runtime.

## Run

```
python3 app.py
```

Serves on `http://127.0.0.1:5050` (macOS reserves port 5000 for AirPlay
Receiver, hence the non-default port). Override with `PORT=8000 python3 app.py`.

Open `http://127.0.0.1:5050/` to see your documents, or go
straight to a page:

```
http://127.0.0.1:5050/annotations?doc=sample&type=pdf&page=1
```


## Adding documents

Easiest: in the document editor (`/document`), use **File → Upload source…**, or
`POST /api/documents/upload` (multipart `file` field) directly — either way the file is
validated (must actually open as a PDF; other types are rejected) and saved under a
slugified id, de-duplicated automatically if that id is already taken.

Files are stored per user, under the owner of the cause they're uploaded for
(`storage/<owner>/documents/`, where `<owner>` is the first 32 hex chars of the SHA-256 of
that owner's lowercased email, recorded in `documents.storage_owner`). A file dropped
into that folder by hand still needs a `documents` row to be visible. Supported types:
- `<doc_id>.pdf` — used directly.
- Only PDFs are supported as source documents. Word files (`.docx`/`.doc`) are not
  accepted: converting them needs LibreOffice, which isn't installed. Convert to PDF
  first.

`doc_id` may only contain letters, numbers, `_` and `-` (no extension, no
slashes) — it's the file's base name.

## Using the viewer

Toolbar modes:
- **Pan** — default, no drawing (scroll/zoom the browser normally).
- **Rectangle** — drag to draw a rectangle annotation in the chosen color.
- **Freehand** — drag to draw a smoothed hand-drawn line/loop.
- **Snippet** — drag a rectangle to immediately crop that region out of the
  page and save it as a PNG (shown in the right-hand sidebar with a download
  link).

**Save annotations** persists the current rectangles/freehand strokes for
that page; they reload automatically next time you open the same page.
**Undo** removes the last-drawn annotation (until saved); **Clear** removes
all of them. Unsaved annotation changes trigger a confirm-before-leaving
browser prompt.

Annotations and snippet rectangles are stored as fractions (0–1) of the page
width/height, so they stay correctly positioned regardless of render
resolution.

## Storage layout

Binary files stay on disk; everything else (annotation shapes, snippet
metadata, reports, cases, causes, allegations) lives in PostgreSQL (see
`db/schema.sql`). Nothing outside `storage.py` — see that file's module
docstring — talks to the database or to these paths directly:

```
storage/<owner>/documents/<doc_id>.<ext>          uploaded source PDFs
storage/<owner>/cache/<doc_id>.pdf                Word -> PDF conversion cache (disposable, never in the database)
storage/<owner>/snippets/<doc_id>__<type>/*.png   extracted snippet images (row per file in the `snippets` table)
```

## API

- `GET  /annotations?doc=<id>&type=pdf&page=<n>` — HTML viewer page.
- `GET  /api/doc/<id>/info?type=pdf` — `{page_count, pages: [{width,height}, ...]}` (PDF points).
- `GET  /api/doc/<id>/render/<page>?type=pdf&dpi=150` — PNG of that page.
- `GET  /api/doc/<id>/annotations/<page>?type=pdf` — list of annotation objects.
- `POST /api/doc/<id>/annotations/<page>?type=pdf` — body `{"annotations": [...]}`, overwrites that page's list.
- `POST /api/doc/<id>/snippet/<page>?type=pdf` — body `{"x","y","w","h"}` fractions (0–1), optional `"dpi"` (default 300). Returns the saved snippet's metadata + URL.
- `GET  /api/doc/<id>/snippets?type=pdf&page=<n>` — list snippets (optionally filtered to one page).
- `DELETE /api/doc/<id>/snippet/<snippet_id>?type=pdf` — delete a snippet.

Annotation object shapes:
```json
{"kind": "rect", "color": "#e02424", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1}
{"kind": "freehand", "color": "#e02424", "points": [[0.1, 0.2], [0.11, 0.21], ...]}
{"kind": "blackout", "color": "#000000", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}
```
