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

## Run

```
python3 app.py
```

Serves on `http://127.0.0.1:5050` (macOS reserves port 5000 for AirPlay
Receiver, hence the non-default port). Override with `PORT=8000 python3 app.py`.

Open `http://127.0.0.1:5050/` to see documents found in `documents/`, or go
straight to a page:

```
http://127.0.0.1:5050/annotations?doc=sample&type=pdf&page=1
```

A demo file `documents/sample.pdf` is included (page 1 = real text layer,
page 2 = image-only, simulating a scanned page).

## Adding documents

Easiest: in the document editor (`/document`), use **File → Upload source…**, or
`POST /api/documents/upload` (multipart `file` field) directly — either way the file is
validated (must actually open as a PDF/Word doc) and saved into `documents/` under a
slugified id, de-duplicated automatically if that id is already taken.

You can also drop files into `documents/` directly:
- `<doc_id>.pdf` — used directly.
- `<doc_id>.docx` / `<doc_id>.doc` — converted to PDF on first request via
  LibreOffice (`soffice --headless --convert-to pdf`), then cached in
  `storage/cache/<doc_id>.pdf`. **LibreOffice is not currently installed** —
  install with `brew install --cask libreoffice`, or convert the file
  yourself and drop the resulting `<doc_id>.pdf` into `documents/`.

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

```
documents/                        source PDFs / Word docs (you add these)
storage/cache/<doc_id>.pdf         Word -> PDF conversion cache
storage/annotations/<doc_id>__<type>.json     {"<page>": [annotation, ...]}
storage/snippets/<doc_id>__<type>.json        snippet metadata list
storage/snippets/<doc_id>__<type>/*.png       extracted snippet images
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
```
