"""Knowledge first: a run starts with what the company already knows about its task.

Measured on 2026-10-01: only 1.8 % of the agents' MCP calls used the knowledge base, though every
agent has the grant. So the start of every run (pos.api_worker start_run) pre-loads the top-k
knowlage passages for the task's title and notes: one cheap search (effort 1, no rerank, a few
seconds, TIMEOUT_S at most), capped at MAX_CHARS (~800 tokens), each passage with its chunk id
(`<doc>:c<n>`) so the result can cite it. The worker puts them into the task part of the prompt
(pos_worker.prompt); the shared guidance says to check knowledge before acting and to cite sources.

Only what helps (2026-10: the same three mailbox passages reached 335 unrelated runs, tainted them all
and caused needless Security holds):
- no pre-load for numbers-only and system roles (SKIP_ROLES: the Access manager's 180 daily reviews)
  nor for tasks from system sources (SYSTEM_SOURCES: access reviews, sentinel and Grafana alerts);
- a relevance threshold: a passage must share at least MIN_SHARED content words with the task
  (knowlage's quick search ranks without a score worth a threshold: every hit is "relevance 0.033");
- our own sources first (INTERNAL_SOURCES: repositories, meetings, PersonalOS);
- a passage from an external source (mail, Drive, web) is listed as a pointer only (its chunk and
  section id, no text): the run is not tainted for passages it never opened. Opening one with the
  `knowledge` tool (mode 'read' with the section id, or a search) taints the run (pos.taint), like
  reading the mail itself would.

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
# Roles whose runs work on numbers and system state, not on documents: no pre-load.
SKIP_ROLES = {"access_manager", "monitor", "deployer", "automation"}
# Task sources that are the system talking (reviews, alerts, invoices import): no pre-load.
SYSTEM_SOURCES = ("access", "event:sentinel", "event:grafana", "sentinel", "grafana", "system", "deployer",
                  "invoices", "scheduler",
                  "review:")  # a review run gets its packet (pos.review_packet), not passages
MIN_SHARED = 2          # content words a passage must share with the task's query
MAX_POINTERS = 3        # external passages listed (id only)
_WORD_RE = re.compile(r"[^\W\d_]{3,}|\b[A-Z][A-Z0-9]+\b", re.UNICODE)
_SECTION_RE = re.compile(r"`([\w.\-]+:s\d+)`")
_HEAD_RE = re.compile(r"^#+ .*$", re.MULTILINE)
STOPWORDS = {
    # Czech
    "jak", "jako", "ale", "ani", "byl", "byla", "bylo", "být", "pro", "při", "pod", "nad", "před", "jen", "jeho",
    "její", "které", "který", "která", "když", "kde", "kdy", "nebo", "než", "této", "tento", "tato", "toto",
    "jsou", "jsem", "jste", "mají", "mít", "také", "tak", "tedy", "aby", "což", "ještě",
    "podle", "mezi", "jsme", "bude", "budou", "více", "všech", "vše", "denní", "týdenní", "úkol",
    # English
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "will", "have", "has", "not",
    "but", "you", "your", "our", "its", "into", "what", "when", "where", "which", "who", "how", "all", "any",
    "each", "per", "via", "task", "purpose", "source", "done", "notes", "daily", "weekly", "check",
}

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


def _terms(text: str) -> set[str]:
    """Content words, folded (case, diacritics) and cut to 5 letters (a crude stem for Czech endings)."""
    import unicodedata

    out = set()
    for w in _WORD_RE.findall(text or ""):
        low = w.lower()
        if low in STOPWORDS:
            continue
        fold = "".join(ch for ch in unicodedata.normalize("NFKD", low) if not unicodedata.combining(ch))
        out.add(fold[:5])
    return out


def shared_terms(query: str, passage: str) -> int:
    """How many of the query's content words the passage has (its header lines left out)."""
    body = _HEAD_RE.sub(" ", passage or "")
    return len(_terms(query) & _terms(body))


def is_internal(p: dict) -> bool:
    return (p.get("source") or "").lower() in INTERNAL_SOURCES


def section_of(p: dict) -> str | None:
    m = _SECTION_RE.search(p.get("text") or "")
    return m.group(1) if m else None


def select(query: str, passages: list[dict]) -> list[dict]:
    """The relevant passages, our own sources first (stable within each group)."""
    need = MIN_SHARED if len(_terms(query)) > MIN_SHARED else 1
    keep = [p for p in passages if shared_terms(query, p["text"]) >= need]
    return sorted(keep, key=lambda p: 0 if is_internal(p) else 1)


def render(passages: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """(the prompt text, the passages shown in full, the external ones listed as pointers). Internal
    passages go in whole (still wrapped as untrusted data); external ones only by id."""
    from .guard.external import wrap_external

    blocks, shown, pointers, used = [], [], [], 0
    for p in passages:
        if not is_internal(p):
            if len(pointers) < MAX_POINTERS:
                pointers.append(p)
            continue
        body = p["text"][:MAX_PASSAGE]
        if used + len(body) > MAX_CHARS:
            continue
        used += len(body)
        shown.append(p)
        blocks.append(wrap_external(f"knowlage:{p['source']}", body, ref=p["chunk"] or p["ref"]))
    if pointers:
        lines = [f"- `{p['chunk'] or p['ref']}` ({p['source']}"
                 + (f", section `{section_of(p)}`" if section_of(p) else "") + ")" for p in pointers]
        blocks.append("Possibly relevant outside documents (not shown; open one with the `knowledge` tool, "
                      "mode 'read' with its section id, only if the task needs it: outside content then "
                      "applies to this run):\n" + "\n".join(lines))
    return "\n\n".join(blocks), shown, pointers


def skip_reason(conn: sqlite3.Connection, ctx: Ctx, task: dict) -> str | None:
    """Why this run gets no pre-load, or None."""
    from . import actors

    role = (actors.get(conn, ctx.actor_id)["role"] or "").lower()
    if role in SKIP_ROLES:
        return f"role {role}"
    source = (task.get("source") or "").lower()
    if source.startswith(SYSTEM_SOURCES):
        return f"system source {source.split(':')[0]}"
    if (task.get("topic") or "") == "chat":
        return "chat"
    return None


def preload(conn: sqlite3.Connection, ctx: Ctx, task: dict, search=None) -> dict | None:
    """The passages for this run's task, or None. `search(query) -> text` is for tests."""
    from . import kb_files, mcp_server

    if search is None and not kb_files.configured():
        return None
    if not mcp_server.may_use(conn, ctx.actor_id, "knowledge"):
        return None
    query = query_for(task)
    if len(query) < MIN_QUERY or skip_reason(conn, ctx, task):
        return None
    try:
        passages = parse((search or _search)(query))
    except Exception as e:  # noqa: BLE001 - fail-open: the run starts without passages
        log.info("knowledge preload for %s failed: %s", task.get("ref"), e)
        return None
    passages = select(query, passages)
    if not passages:
        return None
    text, shown, pointers = render(passages)
    if not text:
        return None
    audit.log(conn, ctx, "knowledge_preload", "task", task.get("id"), chunks=[p["chunk"] for p in shown],
              sources=sorted({p["source"] for p in shown}), pointers=[p["chunk"] for p in pointers],
              external=[])
    # "external": the outside sources whose text the run was shown: none, outside passages are pointers
    # only, so the pre-load never taints a run (opening one through the knowledge tool does).
    return {"text": text, "chunks": [p["chunk"] for p in shown if p["chunk"]], "external": [],
            "pointers": [p["chunk"] for p in pointers if p["chunk"]], "query": query}


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
