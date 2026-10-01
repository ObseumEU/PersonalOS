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
- **Search goes through knowlage.** Files do not keep a second full-text
  index (the old `files_fts` is dropped by migration 18). Code:
  `backend/src/pos/kb_files.py`.
  - *Push.* After an upload is saved, the file is pushed in the background to
    knowlage `POST /api/ingest` (`Authorization: Bearer $POS_KNOWLAGE_API_KEY`)
    as `{source: "personalos", channel: "files", key: "file-<id>", title:
    <name>, text: <name, type, topic, tags + the text extract>}`. The row stores
    the knowlage document id (`kb_doc_id`, `pe_<sha1("personalos:files:file-<id>")[:16]>`,
    the id knowlage derives from the item) and `kb_status` (`pending`, `ok`,
    `error`), `kb_error`, `kb_attempts`, `kb_synced_at`. A failed push never
    fails the upload. The scheduler job `knowlage_files` (every 15 minutes)
    pushes what is `pending` or `error` again; renaming or retagging a file
    marks it `pending`, so knowlage gets the new labels under the same key.
  - *Text.* Text, markdown, CSV and JSON are sent directly. PDFs are sent when
    `pypdf` is installed (`pip install personalos[pdf]`); other files are sent
    with their name and labels only.
  - *Search.* `files.search` asks knowlage's `search` tool (`POST /mcp?workspace=$POS_KNOWLAGE_WORKSPACE`,
    default `firma`, the whole pile; `sources: ["personalos"]`) and maps the hits'
    document ids back to local files, keeping knowlage's order. Visibility,
    topic, tag and archive filters still apply locally. `GET /api/files/search`,
    `GET /api/files?q=`, `GET /api/search` and the MCP `search` tool all use it.
  - *Fallback.* When knowlage is not configured (no `POS_KNOWLAGE_API_KEY`) or
    does not answer, search matches file names only (every word, LIKE) and says
    so: `mode: "filename"` (`files_mode` in `/api/search` and MCP), and the
    Files page shows a note.
  - *Backfill.* Files from before this change have no knowlage document. Push
    them once (idempotent; files that already have a `kb_doc_id` are skipped,
    `--dry-run` only counts):

    ```bash
    python -m pos.kb_files backfill            # inside Docker: docker compose exec api python -m pos.kb_files backfill
    ```

    `python -m pos.kb_files retry` pushes everything `pending` or `error` now,
    instead of waiting for the job.
  - Settings: `POS_KNOWLAGE_URL`, `POS_KNOWLAGE_API_KEY`,
    `POS_KNOWLAGE_WORKSPACE` (where to search, default `firma`),
    `POS_KNOWLAGE_FILES_WORKSPACE` (optional placement of the `files` channel),
    `POS_PUBLIC_URL` (link back to the file, shown with knowlage citations).
- Notes keep their own SQLite FTS5 index (`notes_fts`); a SQLite build without
  FTS5 falls back to LIKE.

## Agents' files and visuals

Code: `backend/src/pos/agent_files.py`, `file_render.py`; web: `src/files/` (FileCard, the lazy
renderers). Agents make files and share them with people; chat (desktop and the phone app) and
Files render them inline. Their own computer, where they make charts and documents with code, is
[SANDBOX.md](SANDBOX.md).

- **Tools** (pos MCP, every agent): `file_create(name, content, mime?, encoding: text|base64,
  project?, topic?, description?)`, `file_update(file_id, content, …)` (a new **version**: the
  bytes of every version stay, Files shows the history and restores any of it),
  `file_read(file_id, version?)` (the text, any version), `file_get` (metadata and extracted
  text), `file_list(scope: mine|all)`, `file_share(file_id, to: owner|<member>|#channel,
  thread_or_task_ref?, message?)` (a chat message, a DM to the owner by default, with the file
  attached), and `attachments` on `chat_send`. `sandbox_share` does the same for a sandbox file.
- **Limits.** `POS_AGENT_FILE_MAX_MB` (20) per file, `POS_AGENT_FILES_QUOTA_MB` (500) for the
  current versions of one agent's files. Names are sanitised (no directories, no traversal).
  An agent's file is owned by the owner, as everything agents file; a private one is also shared
  with the agent that made it, and with whoever it is shared with in chat.
- **Kinds** (`preview`, from the sniffed type and the name): `image` (PNG, JPEG, GIF, WebP),
  `svg`, `pdf`, `markdown`, `csv` (and TSV: a sortable table), `mermaid` (`.mmd`), `dot`
  (`.dot`, `.gv`), `vegalite` (`.vl.json`), `html`, `code` (by extension, highlighted), `text`,
  `download`.
- **Rendering and safety.**
  - SVG is sanitised when stored (`file_render.sanitize_svg`): only SVG drawing elements stay;
    scripts, `foreignObject`, animations, event handlers, `javascript:` and every outside
    reference (`href`, `url()`, `@import`) go; a DOCTYPE with entities is refused. Served as an
    image with a sandboxing CSP and shown with `<img>`.
  - DOT is drawn on the server (`GET /api/files/{id}/render.svg`; graphviz in the api image, 20 s,
    an empty working directory, attributes that read files refused, output sanitised, cached by
    content hash). 422 carries graphviz's message.
  - Mermaid and Vega-Lite render in the browser from a lazy chunk (mermaid in its strict mode;
    Vega with the expression interpreter, so no `eval` under the phone app's CSP; data must be
    inline, never a URL). The phone app's first download stays under its 150 KB budget.
  - HTML is shown in `<iframe sandbox="allow-scripts">` from `GET /api/files/{id}/page`, whose
    CSP adds `sandbox allow-scripts` (an opaque origin even when opened directly: no cookies, no
    PersonalOS API) and `connect-src 'none'` (no network at all).
  - `GET /api/files/{id}/content` serves everything textual as `text/plain`; `?v=<n>` serves an
    earlier version (a chat card shows the version that was shared).
  - A broken diagram or chart comes back to the agent at once as `render_error`.
- **In chat**: a card with the name, kind and version and the preview; a click opens the
  full-screen viewer (zoom and pan for pictures and diagrams, pinch on the phone), download and
  "Otevřít v Souborech".
- **Knowledge.** Agents' files go to knowlage like any file. Private files never do: a file that
  becomes private is deleted from knowlage (`kb_status: private`); search still matches their
  names for whoever may see them.

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
| `GET /api/files/search?q=` | search through knowlage: `{mode: "knowlage" \| "filename", files, error?}` (also `topic`, `tag`, `archived`) |
| `GET/PATCH /api/files/{id}`, `/content`, `/history`, `POST /archive`, `/restore` | one file |
| `GET/POST /api/notes`, `GET/PATCH /api/notes/{id}`, `/history`, `POST /archive`, `/restore` | notes; `/restore` with `{version}` goes back to a version |
| `GET/POST /api/topics`, `GET/PATCH /api/topics/{slug}`, `POST /archive`, `/restore` | topics |
| `GET /api/search?q=` | tasks, files and notes together |

## MCP tools

`search(q)`, `file_get(id)` (metadata and the text extract, wrapped as
outside content; never raw bytes) and `topic_get(slug)` need `tasks:read`;
`note_create` and `note_update` need `tasks:write`.
