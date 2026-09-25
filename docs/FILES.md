# Files, topics and notes

PLAN section 3, items 2, 3 and 6. Code: `backend/src/pos/files.py`, `notes.py`,
`topics.py`, `api_files.py`; web: `pages/Files.tsx`, `Notes.tsx`, `Topics.tsx`.

All three follow the rules of tasks: every write goes through `versioning`
(a version per change, audited), reads follow the visibility layers
(public, team, private), and nothing is deleted, only archived.

## Files

- **Storage.** Bytes live under `POS_DATA_DIR/files` as
  `<yyyy>/<mm>/<sha256 prefix>-<safe name>`; metadata in the `files` table.
  The same content is stored once. Uploading content you can already see
  returns the existing file (`duplicate: true`); an archived one comes back.
- **Limit.** `POS_MAX_UPLOAD_MB` (default 200). Bigger uploads get 413.
- **Security.** The client's name and type are not trusted: names are
  sanitised (no directories, no control characters), the type is sniffed from
  the bytes, stored paths are checked to stay inside the files directory, and
  content is served with `X-Content-Type-Options: nosniff`, a safe
  `Content-Disposition` and (except PDFs) a sandboxing CSP. Text is always
  served as `text/plain`, so nothing uploaded renders as HTML.
- **Preview.** `GET /api/files/{id}/content` is inline for images (PNG, JPEG,
  GIF, WebP), PDF and text; everything else downloads. `?download=1` forces a
  download.
- **Search.** Text, markdown, CSV and JSON are indexed directly. PDFs are
  indexed when `pypdf` is installed (`pip install personalos[pdf]`); without
  it they are stored but not searchable. Search uses SQLite FTS5
  (`files_fts`, `notes_fts`, kept in sync by triggers); a SQLite build without
  FTS5 falls back to LIKE.

## Topics

A topic is the slug that tasks, files and notes carry in `topic` (lower case,
no `#`). Any slug in use is a topic; the `topics` table adds a name,
description and colour. A topic page shows its open and done tasks, files,
notes, and calendar events of the next 30 days whose title or location
mentions it. Archiving a topic hides it; its items stay.

## API

| Path | What |
| --- | --- |
| `GET/POST /api/files` | list (`topic`, `tag`, `q`, `archived`), multipart upload (`file`, `topic`, `tags`, `visibility`) |
| `GET/PATCH /api/files/{id}`, `/content`, `/history`, `POST /archive`, `/restore` | one file |
| `GET/POST /api/notes`, `GET/PATCH /api/notes/{id}`, `/history`, `POST /archive`, `/restore` | notes; `/restore` with `{version}` goes back to a version |
| `GET/POST /api/topics`, `GET/PATCH /api/topics/{slug}`, `POST /archive`, `/restore` | topics |
| `GET /api/search?q=` | tasks, files and notes together |

## MCP tools

`search(q)`, `file_get(id)` (metadata and the text extract, wrapped as
outside content; never raw bytes) and `topic_get(slug)` need `tasks:read`;
`note_create` and `note_update` need `tasks:write`.
