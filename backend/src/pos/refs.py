"""References in agents' text, resolved into what they point at.

Agents write "Poznámka 23", "note:23", "msg 1095", "zpráva 504", chunk ids from the knowledge
base (`firma.gh_0ff08113fa5428ee:c13`, `k834E6OTGTM:c17`, `kniha.gh_…:c0/c2`), "T-431" and
"file:12". For the owner those are pointers to something he cannot see. This module finds them
(`extract`) and fetches what they point at (`resolve`), as the caller may see it:

- note  -> the note's title and full Markdown body;
- msg   -> the message, its author and channel, and the few messages before it;
- chunk -> the knowledge-base document's title, the quoted passage and a link (knowlage
           `read_document`, the passage labelled `[<chunk> · m:ss]`);
- task  -> the task's title and status;
- file  -> the file's name and the start of its text.

The same patterns live in web/src/refs.ts (the chips in task text, notes and chat); the tests
of both use the same examples. Nothing here invents content: a reference that cannot be read
comes back with ok=False and the reason.
"""

import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict

from .core import Ctx, Forbidden, NotFound

log = logging.getLogger("pos.refs")

KINDS = ("note", "msg", "chunk", "task", "file")

# A chunk id: an optional workspace prefix ("firma.", "kniha."), a document id of 6+ characters
# (a YouTube id, gh_<hash>, pe_<hash>) and ":c<n>", optionally more chunks of the same document
# ("…:c0/c2"). Not inside a URL or a path.
_CHUNK = re.compile(r"(?<![\w/.:#-])((?:[a-z][a-z0-9]{1,20}\.)?[A-Za-z0-9_-]{6,64}):c(\d{1,5})((?:/c\d{1,5})*)\b")
_NOTE = re.compile(
    r"(?i)(?<![\w-])(?:note|notes|pozn(?:á|a)m(?:k|ek)\w*|pozn\.)\s*(?::|#|č\.)?\s*\**\s*#?(\d{1,6})\**"
    r"((?:\s*(?:,|a|and|i|/)\s*\**#?\d{1,6}\**)*)")
_MSG = re.compile(
    r"(?i)(?<![\w-])(?:(?:msg|message|messages)\s*(?::|#|č\.)?\s*\**#?(\d{1,9})|zpr(?:á|a)v\w*\s*(?:#|č\.\s*)?\**(\d{3,9}))\b")
_TASK = re.compile(r"(?<![\w-])T-(\d{1,6})\b")
_FILE = re.compile(r"(?i)(?<![\w-])(?:file\s*[:#]\s*|soubor\w*\s+#)(\d{1,9})\b")


def _numbers(head: str, tail: str) -> list[str]:
    return [head, *re.findall(r"\d{1,9}", tail or "")]


def extract(text: str) -> list[dict]:
    """Every reference in the text, in order, without duplicates: [{kind, id, raw}]."""
    found: list[tuple[int, str, str, str]] = []
    text = text or ""
    for m in _CHUNK.finditer(text):
        doc = m.group(1)
        if doc.lower().startswith(("http", "www")) or re.fullmatch(r"T-\d+", doc):
            continue
        for n in [m.group(2), *re.findall(r"\d+", m.group(3) or "")]:
            found.append((m.start(), "chunk", f"{doc}:c{n}", m.group(0)))
    for m in _NOTE.finditer(text):
        for n in _numbers(m.group(1), m.group(2)):
            found.append((m.start(), "note", n, m.group(0)))
    for m in _MSG.finditer(text):
        found.append((m.start(), "msg", m.group(1) or m.group(2), m.group(0)))
    for m in _TASK.finditer(text):
        found.append((m.start(), "task", f"T-{int(m.group(1))}", m.group(0)))
    for m in _FILE.finditer(text):
        found.append((m.start(), "file", m.group(1), m.group(0)))
    out, seen = [], set()
    for _, kind, rid, raw in sorted(found, key=lambda x: x[0]):
        if (kind, rid) in seen:
            continue
        seen.add((kind, rid))
        out.append({"kind": kind, "id": rid, "raw": raw.strip()})
    return out


def parse(ref: str) -> tuple[str, str]:
    """'note:23', 'msg:1095', 'task:T-4', 'chunk:k834E6OTGTM:c17', 'file:3' -> (kind, id)."""
    kind, _, rid = (ref or "").partition(":")
    if kind not in KINDS or not rid:
        raise NotFound(f"unknown reference {ref!r}")
    return kind, rid


# ------------------------------------------------------------------ resolving

def _fail(kind: str, rid: str, why: str) -> dict:
    return {"kind": kind, "id": rid, "ok": False, "error": why}


def _note(conn: sqlite3.Connection, ctx: Ctx, rid: str) -> dict:
    from . import notes

    n = notes.get(conn, ctx, int(rid))
    return {"kind": "note", "id": rid, "ok": True, "title": n["title"], "text": n.get("body") or "",
            "updated_at": n.get("updated_at"), "link": f"/notes?note={n['id']}",
            "archived": bool(n.get("archived_at"))}


def channel_label(conn: sqlite3.Connection, ch: sqlite3.Row) -> str:
    if ch["kind"] == "dm":
        names = [r[0] for r in conn.execute(
            "SELECT a.name FROM channel_members m JOIN actors a ON a.id = m.actor_id WHERE m.channel_id = ? "
            "ORDER BY a.name", (ch["id"],))]
        return "Soukromá zpráva " + " ↔ ".join(names) if names else "Soukromá zpráva"
    return f"#{ch['name']}" if ch["name"] else f"kanál {ch['id']}"


def _msg(conn: sqlite3.Connection, ctx: Ctx, rid: str, context: int = 3) -> dict:
    from . import chat

    m = conn.execute("""SELECT m.*, a.name AS author FROM chat_messages m JOIN actors a ON a.id = m.author_id
                        WHERE m.id = ?""", (int(rid),)).fetchone()
    if m is None or m["archived_at"]:
        raise NotFound(f"message {rid}")
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (m["channel_id"],)).fetchone()
    if not chat.can_read(conn, ch, ctx.actor_id):
        raise Forbidden(f"message {rid} is in a private channel")
    before = conn.execute("""SELECT m.id, m.body, m.created_at, a.name AS author FROM chat_messages m
                             JOIN actors a ON a.id = m.author_id
                             WHERE m.channel_id = ? AND m.id < ? AND m.archived_at IS NULL
                             ORDER BY m.id DESC LIMIT ?""", (ch["id"], m["id"], context)).fetchall()
    where = channel_label(conn, ch)
    return {"kind": "msg", "id": rid, "ok": True, "title": f"{m['author']} · {where}", "author": m["author"],
            "channel": where, "channel_id": ch["id"], "text": m["body"], "at": m["created_at"],
            "context": [{"id": r["id"], "author": r["author"], "text": r["body"], "at": r["created_at"]}
                        for r in reversed(before)],
            "link": f"/chat?c={ch['id']}" + (f"&t={m['reply_to']}" if m["reply_to"] else "")}


def _task(conn: sqlite3.Connection, ctx: Ctx, rid: str) -> dict:
    from . import tasks

    t = tasks.get(conn, ctx, tasks.parse_id(rid))
    return {"kind": "task", "id": t["ref"], "ok": True, "title": t["title"], "status": t["status"],
            "assignee": t.get("assignee_name"), "text": (t.get("notes") or "")[:4000],
            "result": (t.get("progress_note") or "")[:4000], "link": f"/tasks/{t['ref']}"}


def _file(conn: sqlite3.Connection, ctx: Ctx, rid: str) -> dict:
    from . import files

    f = files.get(conn, ctx, int(rid), with_text=True)
    text = f.get("text_extract") or ""
    return {"kind": "file", "id": rid, "ok": True, "title": f["name"], "mime": f.get("mime"),
            "text": text[:6000], "truncated": len(text) > 6000, "link": f"/files?file={f['id']}",
            "download": f"/api/files/{f['id']}/download"}


# Knowledge-base documents, kept a while: one report quotes several chunks of the same document.
_DOCS: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_DOCS_LOCK = threading.Lock()
DOC_TTL_S = 6 * 3600


def _read_document(doc_id: str) -> str:
    """knowlage read_document; a workspace-prefixed id ("kniha.gh_…") is read in that workspace."""
    import os

    from . import kb_files

    with _DOCS_LOCK:
        hit = _DOCS.get(doc_id)
        if hit and time.time() - hit[0] < DOC_TTL_S:
            return hit[1]
    m = re.match(r"([a-z][a-z0-9]{1,20})\.(.+)$", doc_id)
    ws = m.group(1) if m else os.environ.get("POS_KNOWLAGE_WORKSPACE", "firma")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_document", "arguments": {"doc_id": doc_id}}}
    r = kb_files._request("POST", "/mcp", params={"workspace": ws}, json=call, timeout=60,
                          headers={"Accept": "application/json, text/event-stream"})
    text = kb_files._tool_text(r)
    with _DOCS_LOCK:
        _DOCS[doc_id] = (time.time(), text)
        while len(_DOCS) > 48:
            _DOCS.popitem(last=False)
    return text


def pretty_title(title: str) -> str:
    """A document's title for people: a repo path becomes its file name without the date and the
    extension ("AlexHormozi/transcripts/videos/20211025 - Explaining … [neTSqOAMgao].md" ->
    "Explaining … [neTSqOAMgao]")."""
    t = title.strip()
    if "/" in t:
        t = t.rsplit("/", 1)[1]
    t = re.sub(r"\.(md|txt|markdown)$", "", t)
    t = re.sub(r"^\d{8}\s*-\s*", "", t)
    return t.replace("-", " ").strip() if re.fullmatch(r"[\w-]+", t) else t


def chunk_from_document(doc: str, chunk_id: str) -> dict | None:
    """{title, quote, link, at} of one chunk in a read_document text; None when it is not there.
    Labels look like `[<chunk> · 33:39]` (a recording) or `[<chunk> · ř. 101–107]` (a file's lines)."""
    doc = re.sub(r"</?external\b[^>]*>", "", doc)
    title = next((ln[2:].strip() for ln in doc.splitlines() if ln.startswith("# ")), "")
    url = re.search(r"\burl:\s*(\S+)", doc)
    m = re.search(r"\[" + re.escape(chunk_id) + r"(?:\s*·\s*([^\]]*))?\]\s*", doc)
    if not m:
        return None
    rest = doc[m.end():]
    end = re.search(r"\n\s*\[[^\]\s]+:c\d+[^\]]*\]|\n#{1,6} `", rest)  # the next chunk or section
    quote = (rest[:end.start()] if end else rest).strip()
    quote = re.sub(r"⟨[0-9:]+⟩\s*", "", quote)
    label = (m.group(1) or "").strip()
    link = url.group(1) if url else None
    if link and re.fullmatch(r"\d{1,2}(?::\d{2}){1,2}", label) and re.search(r"youtu\.?be", link):
        secs = 0
        for part in label.split(":"):
            secs = secs * 60 + int(part)
        link += ("&" if "?" in link else "?") + f"t={secs}"
    lines = re.search(r"(\d+)\s*[–-]\s*(\d+)", label)
    if link and lines and "github.com" in link and "#" not in link:
        link += f"#L{lines.group(1)}-L{lines.group(2)}"
    return {"title": pretty_title(title) or chunk_id.rsplit(":", 1)[0], "quote": quote[:1500],
            "truncated": len(quote) > 1500, "link": link, "at": label or None}


def _chunk(rid: str) -> dict:
    from . import kb_files

    if not kb_files.configured():
        return _fail("chunk", rid, "znalostní báze není na tomhle serveru připojená")
    doc_id = rid.rsplit(":c", 1)[0]
    try:
        doc = _read_document(doc_id)
    except kb_files.Unavailable as e:
        return _fail("chunk", rid, f"dokument ve znalostní bázi nenalezen ({str(e)[:120]})")
    got = chunk_from_document(doc, rid)
    if got is None:
        return _fail("chunk", rid, "pasáž v dokumentu nenalezena")
    return {"kind": "chunk", "id": rid, "ok": True, "text": got["quote"], **got}


def resolve(conn: sqlite3.Connection, ctx: Ctx, kind: str, rid: str) -> dict:
    """What one reference points at, as the caller may see it; ok=False with the reason otherwise."""
    try:
        if kind == "note":
            return _note(conn, ctx, rid)
        if kind == "msg":
            return _msg(conn, ctx, rid)
        if kind == "task":
            return _task(conn, ctx, rid)
        if kind == "file":
            return _file(conn, ctx, rid)
        if kind == "chunk":
            return _chunk(rid)
    except NotFound:
        return _fail(kind, rid, "neexistuje")
    except Forbidden:
        return _fail(kind, rid, "nemáš k tomu přístup")
    except Exception as e:  # noqa: BLE001 - a preview never breaks the page
        log.info("resolving %s:%s failed: %s", kind, rid, e)
        return _fail(kind, rid, "nepodařilo se načíst")
    return _fail(kind, rid, "neznámý typ odkazu")


def resolve_all(conn: sqlite3.Connection, ctx: Ctx, refs: list[dict], *, skip: set[tuple[str, str]] = frozenset(),
                limit: int = 40) -> list[dict]:
    out = []
    for r in refs[:limit]:
        if (r["kind"], r["id"]) in skip:
            continue
        got = resolve(conn, ctx, r["kind"], r["id"])
        got["raw"] = r.get("raw")
        out.append(got)
    return out


def dumps(resolved: list[dict]) -> str:
    return json.dumps(resolved, ensure_ascii=False)
