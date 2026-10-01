"""Knowledge first: a run starts with what the company already knows about its task.

Measured on 2026-10-01: only 1.8 % of the agents' MCP calls used the knowledge base, though every
agent has the grant. So the start of every run (pos.api_worker start_run) pre-loads the top-k
knowlage passages for the task's title and notes: one cheap search (effort 1, no rerank, a few
seconds, TIMEOUT_S at most), capped at MAX_CHARS (~800 tokens), each passage with its chunk id
(`<doc>:c<n>`) so the result can cite it. The worker puts them into the task part of the prompt
(pos_worker.prompt); the shared guidance says to check knowledge before acting and to cite sources.

The passages are outside content, wrapped as untrusted: a passage from an external source (mail,
web, Drive) taints the run (pos.taint), like reading the mail itself would.

Fail-open: knowlage down, slow or not configured, or an agent without tool:knowledge: no passages,
the run starts as before. Each pre-load leaves an audit line `knowledge_preload` (the measure:
`share`).
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import audit
from .core import Ctx

log = logging.getLogger(__name__)

K = 4
EFFORT = 1
TIMEOUT_S = 8
MAX_CHARS = 3200
MAX_PASSAGE = 900
MIN_QUERY = 12
# knowlage sources whose documents are ours (our repositories, our transcripts and uploads):
# everything else (mailbox, gdrive, web, …) is external and taints the run.
INTERNAL_SOURCES = {"github", "file", "meeting", "meetings", "personalos"}

_BLOCK_RE = re.compile(r"<external ([^>]*)>(.*?)</external>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_CHUNK_RE = re.compile(r"`([\w.\-]+:c\d+)`")
_EXT_RE = re.compile(r"<external [^>]*>.*?</external>", re.DOTALL)


def query_for(task: dict) -> str:
    """The search query: the title and the start of the notes, without outside content and markup."""
    notes = _EXT_RE.sub(" ", task.get("notes") or "")
    notes = re.sub(r"[#*`>_\[\]()|-]+", " ", notes)
    q = f"{task.get('title') or ''}. {notes}"
    return re.sub(r"\s+", " ", q).strip()[:300]


def parse(text: str) -> list[dict]:
    """knowlage's search answer → passages {chunk, source, ref, text}."""
    out = []
    for attrs, body in _BLOCK_RE.findall(text or ""):
        a = dict(_ATTR_RE.findall(attrs))
        chunk = _CHUNK_RE.search(body)
        out.append({"chunk": chunk.group(1) if chunk else None, "source": a.get("source") or "unknown",
                    "ref": a.get("ref"), "text": body.strip()})
    return out


def _search(query: str) -> str:
    from . import kb_files

    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": query, "k": K, "effort": EFFORT}}}
    r = kb_files._request("POST", "/mcp", params={"workspace": os.environ.get("POS_KNOWLAGE_WORKSPACE", "firma")},
                          json=call, timeout=TIMEOUT_S, headers={"Accept": "application/json, text/event-stream"})
    return kb_files._tool_text(r)


def render(passages: list[dict]) -> str:
    from .guard.external import wrap_external

    blocks, used = [], 0
    for p in passages:
        body = p["text"][:MAX_PASSAGE]
        if used + len(body) > MAX_CHARS:
            break
        used += len(body)
        blocks.append(wrap_external(f"knowlage:{p['source']}", body, ref=p["chunk"] or p["ref"]))
    return "\n\n".join(blocks)


def preload(conn: sqlite3.Connection, ctx: Ctx, task: dict, search=None) -> dict | None:
    """The passages for this run's task, or None. `search(query) -> text` is for tests."""
    from . import kb_files, mcp_server

    if search is None and not kb_files.configured():
        return None
    if not mcp_server.may_use(conn, ctx.actor_id, "knowledge"):
        return None
    query = query_for(task)
    if len(query) < MIN_QUERY or (task.get("topic") or "") == "chat":
        return None
    try:
        passages = parse((search or _search)(query))
    except Exception as e:  # noqa: BLE001 - fail-open: the run starts without passages
        log.info("knowledge preload for %s failed: %s", task.get("ref"), e)
        return None
    if not passages:
        return None
    text = render(passages)
    shown = passages[:text.count("<external ")]
    external = sorted({p["source"] for p in shown if p["source"].lower() not in INTERNAL_SOURCES})
    audit.log(conn, ctx, "knowledge_preload", "task", task.get("id"), chunks=[p["chunk"] for p in shown],
              sources=sorted({p["source"] for p in shown}), external=external)
    return {"text": text, "chunks": [p["chunk"] for p in shown if p["chunk"]], "external": external,
            "query": query}


def share(conn: sqlite3.Connection, days: int = 7, now: datetime | None = None) -> dict:
    """The measure: of the agents' runs in the last `days`, how many started with passages, how many
    used the knowledge tool themselves, and the knowledge tool's share of all MCP calls."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    runs = conn.execute("""SELECT COUNT(*) FROM runs r JOIN actors a ON a.id = r.actor_id
                           WHERE r.started_at >= ? AND a.kind != 'human'""", (since,)).fetchone()[0]
    preloaded = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'knowledge_preload' AND at >= ?",
                             (since,)).fetchone()[0]
    calls = conn.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(action = 'mcp:knowledge'), 0) AS k FROM audit_log
                            WHERE action LIKE 'mcp:%' AND action NOT LIKE 'mcp:%:%' AND at >= ?""",
                         (since,)).fetchone()
    return {"days": days, "runs": runs, "runs_with_passages": preloaded,
            "preload_share": round(preloaded / runs, 3) if runs else None,
            "knowledge_calls": calls["k"], "mcp_calls": calls["n"],
            "knowledge_call_share": round(calls["k"] / calls["n"], 3) if calls["n"] else None}
