"""Files in knowlage: PersonalOS keeps the bytes, knowlage indexes and searches them.

The Files section does not keep a second full-text index. Every uploaded file
is pushed into knowlage (POST /api/ingest) as a document of source
`personalos`, channel `files`, key `file-<id>`; the file row remembers the
knowlage document id and how pushing went (kb_status: pending, ok, error;
kb_error, kb_attempts, kb_synced_at). Pushing is best-effort and never blocks
an upload: it runs after the upload is saved, a scheduler job retries what
failed, and `python -m pos.kb_files backfill` pushes files that never had a
knowlage document.

Searching files asks knowlage's `search` tool (its MCP endpoint, filtered to
source `personalos`) and maps the hits back to local files by document id.

knowlage does not return ids from /api/ingest; it derives them from the item:
`pe_` + sha1("personalos:<channel>:<key>")[:16], prefixed with `<workspace>.`
outside its default workspace. We store the unprefixed id and compare hits
without the workspace prefix.

Settings: POS_KNOWLAGE_URL, POS_KNOWLAGE_API_KEY (as in pos.knowledge; without
the key nothing is pushed and search falls back to file names),
POS_KNOWLAGE_WORKSPACE (where to search, default `firma`: the company-wide pile
that holds every document), POS_KNOWLAGE_FILES_WORKSPACE (optional placement
of the `files` channel), POS_PUBLIC_URL (link back to the file).
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

import httpx

from .core import now_iso
from .knowledge import _headers, base_url

log = logging.getLogger(__name__)

SOURCE = "personalos"
CHANNEL = "files"
MAX_ATTEMPTS = 20
_transport: httpx.BaseTransport | None = None  # tests put a mock transport here


class Unavailable(Exception):
    """knowlage is not configured, not reachable, or answered with an error."""


def configured() -> bool:
    return bool(os.environ.get("POS_KNOWLAGE_API_KEY")) and os.environ.get("POS_KNOWLAGE_DISABLED") != "1"


def item_key(file_id: int) -> str:
    return f"file-{file_id}"


def doc_id(file_id: int) -> str:
    """knowlage's id for a pushed file (kb.connectors.item_doc_id, default workspace form)."""
    return "pe_" + hashlib.sha1(f"{SOURCE}:{CHANNEL}:{item_key(file_id)}".encode()).hexdigest()[:16]


def base_id(kb_id: str) -> str:
    """A knowlage document id without its workspace prefix (`firma.pe_…` → `pe_…`)."""
    return kb_id.rsplit(".", 1)[-1]


def _request(method: str, path: str, **kw) -> httpx.Response:
    if not configured():
        raise Unavailable("knowlage is not configured (POS_KNOWLAGE_API_KEY)")
    try:
        with httpx.Client(transport=_transport, timeout=kw.pop("timeout", 30), follow_redirects=True) as client:
            r = client.request(method, base_url() + path, headers={**_headers(), **kw.pop("headers", {})}, **kw)
    except httpx.HTTPError as e:
        raise Unavailable(f"knowlage is unreachable: {e}"[:300]) from e
    if r.status_code >= 400:
        raise Unavailable(f"knowlage answered {r.status_code}: {r.text[:200]}")
    return r


# ------------------------------------------------------------------ push

def item(row: sqlite3.Row | dict) -> dict:
    """The /api/ingest item for one file: its name and labels, then its text."""
    tags = json.loads(row["tags"] or "[]")
    head = [f"File in PersonalOS: {row['name']}", f"Type: {row['mime'] or 'unknown'}"]
    if row["topic"]:
        head.append(f"Topic: {row['topic']}")
    if "description" in row.keys() and row["description"]:
        head.append(f"Description: {row['description']}")
    if tags:
        head.append("Tags: " + ", ".join(tags))
    text = "\n".join(head) + "\n\n" + (row["text_extract"] or "")
    out = {"source": SOURCE, "key": item_key(row["id"]), "channel": CHANNEL, "title": row["name"],
           "text": text.strip(), "date": (row["created_at"] or "")[:10] or None}
    if public := os.environ.get("POS_PUBLIC_URL"):
        out["url"] = f"{public.rstrip('/')}/files?file={row['id']}"
    if ws := os.environ.get("POS_KNOWLAGE_FILES_WORKSPACE"):
        out["workspace"] = ws
    return out


def ingest_file(conn: sqlite3.Connection, file_id: int) -> dict:
    """Push one file into knowlage and record the outcome on its row (committed).
    Never raises for knowlage trouble; returns {"ok": bool, ...}."""
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"no file {file_id}"}
    if not configured():
        return {"ok": False, "skipped": "knowlage is not configured"}
    if row["visibility"] == "private":
        return _withdraw(conn, row)
    try:
        r = _request("POST", "/api/ingest", json={"items": [item(row)]}, timeout=60)
        report = r.json()
        if not isinstance(report, dict) or not {"added", "updated", "unchanged"} & set(report):
            raise Unavailable(f"unexpected answer from knowlage: {str(report)[:200]}")
    except (Unavailable, ValueError) as e:
        conn.execute("UPDATE files SET kb_status = 'error', kb_error = ?, kb_attempts = kb_attempts + 1 WHERE id = ?",
                     (str(e)[:500], file_id))
        conn.commit()
        return {"ok": False, "error": str(e)[:500]}
    kid = doc_id(file_id)
    conn.execute("""UPDATE files SET kb_doc_id = ?, kb_status = 'ok', kb_error = NULL, kb_attempts = kb_attempts + 1,
                    kb_synced_at = ? WHERE id = ?""", (kid, now_iso(), file_id))
    conn.commit()
    return {"ok": True, "kb_doc_id": kid}


def _withdraw(conn: sqlite3.Connection, row) -> dict:
    """A private file stays out of the shared knowledge base: never pushed, and taken out
    again when it was pushed before it became private (kb_status 'private')."""
    if row["kb_doc_id"]:
        try:
            _request("POST", "/api/ingest", json={"items": [], "deletes": [
                {"source": SOURCE, "channel": CHANNEL, "key": item_key(row["id"])}]}, timeout=60)
        except Unavailable as e:
            conn.execute("UPDATE files SET kb_status = 'error', kb_error = ?, kb_attempts = kb_attempts + 1 "
                         "WHERE id = ?", (str(e)[:500], row["id"]))
            conn.commit()
            return {"ok": False, "error": str(e)[:500]}
    conn.execute("""UPDATE files SET kb_doc_id = NULL, kb_status = 'private', kb_error = NULL, kb_synced_at = ?
                    WHERE id = ?""", (now_iso(), row["id"]))
    conn.commit()
    return {"ok": True, "private": True}


def ingest_in_background(db_path: Path, file_id: int) -> None:
    """After an upload: push the file, whatever happens the upload stays done."""
    from .db import connect

    try:
        conn = connect(db_path)
        try:
            ingest_file(conn, file_id)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - best effort; the retry job picks it up
        log.exception("pushing file %s into knowlage failed", file_id)


def mark_changed(conn: sqlite3.Connection, file_id: int) -> None:
    """Name, topic, tags, content or visibility changed: knowlage's copy is stale until pushed again
    (a file that became private is taken out of it)."""
    conn.execute("UPDATE files SET kb_status = 'pending', kb_attempts = 0 WHERE id = ? AND kb_status IS NOT NULL",
                 (file_id,))


def sync_pending(conn: sqlite3.Connection, limit: int = 25) -> dict:
    """The retry job: push files whose last push is pending or failed."""
    if not configured():
        return {"skipped": "knowlage is not configured"}
    rows = conn.execute(
        """SELECT id FROM files WHERE archived_at IS NULL AND kb_status IN ('pending', 'error')
           AND kb_attempts < ? ORDER BY id LIMIT ?""", (MAX_ATTEMPTS, limit)).fetchall()
    results = [ingest_file(conn, r["id"]) for r in rows]
    return {"pushed": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)}


def backfill(conn: sqlite3.Connection, *, apply: bool = True, limit: int | None = None,
             include_archived: bool = False) -> dict:
    """Push every file that has no knowlage document yet. Idempotent: pushed files
    have an id and are skipped next time; knowlage updates by key anyway."""
    sql = "SELECT id FROM files WHERE kb_doc_id IS NULL AND visibility != 'private'"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    ids = [r["id"] for r in conn.execute(sql + " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))]
    if not apply:
        return {"would_push": len(ids), "configured": configured()}
    if not configured():
        return {"error": "knowlage is not configured (POS_KNOWLAGE_API_KEY)", "missing": len(ids)}
    out = {"pushed": 0, "failed": 0, "errors": []}
    for fid in ids:
        res = ingest_file(conn, fid)
        if res["ok"]:
            out["pushed"] += 1
        else:
            out["failed"] += 1
            out["errors"].append({"file_id": fid, "error": res.get("error")})
    out["errors"] = out["errors"][:20]
    return out


# ------------------------------------------------------------------ search

_REF = re.compile(r'<external [^>]*\bref="([^"]+)"')


def _tool_text(r: httpx.Response) -> str:
    """The text of an MCP tools/call answer (JSON, or SSE with the JSON in `data:`)."""
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        body = json.loads(data[-1]) if data else {}
    else:
        body = r.json()
    if "error" in body:
        raise Unavailable(f"knowlage search failed: {str(body['error'])[:200]}")
    result = body.get("result") or {}
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError") or text.startswith("CHYBA"):
        raise Unavailable(f"knowlage search failed: {text[:200]}")
    return text


def search(q: str, limit: int = 40) -> list[str]:
    """knowlage document ids (without workspace prefix) of PersonalOS files matching
    `q`, best first. Raises Unavailable when knowlage cannot answer."""
    ws = os.environ.get("POS_KNOWLAGE_WORKSPACE", "firma")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": q, "k": max(1, min(limit, 40)),
                                                       "sources": [SOURCE], "kinds": ["chunk"]}}}
    r = _request("POST", "/mcp", params={"workspace": ws}, json=call, timeout=30,
                 headers={"Accept": "application/json, text/event-stream"})
    try:
        text = _tool_text(r)
    except (ValueError, AttributeError) as e:
        raise Unavailable(f"unexpected answer from knowlage search: {e}"[:300]) from e
    out: list[str] = []
    for ref in _REF.findall(text):
        b = base_id(ref)
        if b not in out:
            out.append(b)
    return out


# ------------------------------------------------------------------ command line

def main(argv: list[str] | None = None) -> int:
    from .config import get_settings
    from .db import connect, migrate

    parser = argparse.ArgumentParser(prog="pos.kb_files")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="push files without a knowlage document into knowlage (idempotent)")
    b.add_argument("--dry-run", action="store_true", help="only count")
    b.add_argument("--db", help="path to personalos.db (default: POS_DATA_DIR/personalos.db)")
    b.add_argument("--limit", type=int)
    b.add_argument("--include-archived", action="store_true")
    r = sub.add_parser("retry", help="push files whose last push is pending or failed")
    r.add_argument("--db")
    args = parser.parse_args(argv)
    conn = connect(Path(args.db) if args.db else get_settings().db_path)
    try:
        migrate(conn)
        if args.cmd == "backfill":
            out = backfill(conn, apply=not args.dry_run, limit=args.limit, include_archived=args.include_archived)
        else:
            out = sync_pending(conn, limit=10_000)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
