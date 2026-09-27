"""Our knowledge base (knowlage-agent) as seen from PersonalOS: the graph of its
workspaces, sources, collections and documents, and questions answered with
verified citations.

knowlage has no graph API of its own, so the graph is built here from its read
API (/api/workspaces, /api/sources, /api/documents) and cached for a few
minutes. When knowlage is unreachable the screens get `available: false` and
show that, instead of breaking.

Settings: POS_KNOWLAGE_URL (default https://knowlage.obseum.cz),
POS_KNOWLAGE_API_KEY when knowlage runs with KB_API_KEY.

Effort (knowlage's levels 1–6, or their names): a person's explicit question
keeps knowlage's default 3 (Standard); automatic lookups by agents use
AGENT_LOOKUP_EFFORT (2, Rychle: one reranked search, seconds).
"""

import json
import os
import threading
import time
from collections import defaultdict

import httpx

CACHE_S = 300
AGENT_LOOKUP_EFFORT = 2  # knowlage "Rychle": what agents ask with unless they choose a level
_cache: dict = {}
_lock = threading.Lock()


def base_url() -> str:
    return os.environ.get("POS_KNOWLAGE_URL", "https://knowlage.obseum.cz").rstrip("/")


def public_url() -> str:
    """The address people open (links in the UI); the API may reach knowlage another way."""
    return os.environ.get("POS_KNOWLAGE_PUBLIC_URL", "https://knowlage.obseum.cz").rstrip("/")


def _headers() -> dict:
    key = os.environ.get("POS_KNOWLAGE_API_KEY")
    return {"Authorization": f"Bearer {key}", "X-KB-Actor": "agent:personalos"} if key else \
        {"X-KB-Actor": "agent:personalos"}


def _get(path: str, timeout: float = 20) -> object:
    r = httpx.get(base_url() + path, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def _collection(doc: dict) -> str:
    """The group a document belongs to: its YouTube channel or GitHub repository."""
    if doc.get("channel"):
        return str(doc["channel"])
    src = str(doc.get("source") or "")
    if src.startswith("github:"):
        return src.split(":")[1]
    return src.split("/")[0] if "/" in src else (doc.get("origin") or "files")


def build_graph(workspaces: list[dict], sources: list[dict], documents: list[dict], *,
                docs_per_collection: int = 3, max_collections: int = 40, sizes: dict | None = None) -> dict:
    """Nodes {id, label, type, workspace, source, url, weight, count, chars} and edges
    {source, target, type, weight}: workspace → source → collection → document.

    `count` is how many documents a node holds, over ALL documents (rolled up
    collection → source → workspace), so the graph can size nodes by content;
    `sizes` (knowlage's /api/sizes, when it has it) is preferred over counting
    the document list. Only the biggest collections and their newest documents
    become nodes."""
    docs = [d for d in documents if d.get("status") != "error" and d.get("workspace")]
    repo_source = {t["target"]: s["id"] for s in sources for t in s.get("targets", [])}

    def source_of(coll: str, origin: str | None) -> str:
        return repo_source.get(coll) or ("files" if origin in (None, "file") else origin)

    # Every collection's size: (workspace, collection) → {count, chars, source, labels}.
    groups: dict[tuple[str, str], dict] = {}
    if sizes and sizes.get("groups"):
        # New knowlage (one pile): groups are counted once in the company
        # workspace; project workspaces are labels on them.
        company = sizes.get("company") or "firma"
        for g in sizes["groups"]:
            coll = g.get("channel") or "Files"
            ws = g.get("workspace") or company
            groups[(ws, coll)] = {"count": int(g.get("documents") or 0), "chars": int(g.get("chars") or 0),
                                  "source": source_of(coll, g.get("origin")),
                                  "labels": {k: int(v) for k, v in (g.get("labels") or {}).items() if k != ws}}
    else:
        for d in docs:
            coll = _collection(d)
            g = groups.setdefault((d["workspace"], coll), {"count": 0, "chars": 0,
                                                           "source": source_of(coll, d.get("origin"))})
            g["count"] += 1
            g["chars"] += int(d.get("chars") or 0)
    per_collection: dict[tuple[str, str], list[dict]] = defaultdict(list)
    company = (sizes or {}).get("company")
    seen: set[str] = set()
    for d in docs:
        if company and d["id"] in seen:
            continue
        seen.add(d["id"])
        per_collection[(company or d["workspace"], _collection(d))].append(d)

    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict] = {}

    def node(nid: str, label: str, kind: str, **kw) -> dict:
        n = nodes.setdefault(nid, {"id": nid, "label": label, "type": kind, "workspace": kw.get("workspace"),
                                   "source": kw.get("source"), "url": kw.get("url"), "weight": 0, "count": 0,
                                   "chars": 0})
        n["count"] += kw.get("count", 0)
        n["chars"] += kw.get("chars", 0)
        n["weight"] = n["count"]
        return n

    def edge(a: str, b: str, w: int) -> None:
        e = edges.setdefault((a, b), {"source": a, "target": b, "type": "contains", "weight": 0})
        e["weight"] += w

    for w in workspaces:
        node(f"ws:{w['id']}", w.get("name") or w["id"], "workspace", workspace=w["id"])
    labelled = bool(sizes and sizes.get("company"))
    names = {s["id"]: s.get("name") or s["id"] for s in sources}
    # Roll every collection up into its source and workspace, shown or not.
    for (ws, coll), g in groups.items():
        src = f"src:{g['source']}"
        node(src, "Files and videos" if g["source"] == "files" else names.get(g["source"], g["source"]), "source",
             source=g["source"], count=g["count"], chars=g["chars"])
        if f"ws:{ws}" in nodes:
            nodes[f"ws:{ws}"]["count"] += g["count"]
            nodes[f"ws:{ws}"]["chars"] += g["chars"]
            nodes[f"ws:{ws}"]["weight"] = nodes[f"ws:{ws}"]["count"]
        edge(f"ws:{ws}", src, g["count"])
        for label, n in g.get("labels", {}).items():  # a project workspace holds part of it
            if f"ws:{label}" in nodes and not labelled:
                nodes[f"ws:{label}"]["count"] += n
    if labelled:  # workspace sizes as knowlage counts them
        for w in sizes.get("workspaces") or []:
            if f"ws:{w['workspace']}" in nodes:
                nodes[f"ws:{w['workspace']}"].update(count=int(w.get("documents") or 0),
                                                     chars=int(w.get("chars") or 0),
                                                     weight=int(w.get("documents") or 0))
    top = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)[:max_collections]
    for (ws, coll), g in top:
        cid = f"col:{ws}:{coll}"
        node(cid, coll, "collection", workspace=ws, source=g["source"], count=g["count"], chars=g["chars"],
             url=f"https://github.com/{coll}" if g["source"] != "files" and "/" in coll else None)
        edge(f"src:{g['source']}", cid, g["count"])
        for label, n in g.get("labels", {}).items():
            if f"ws:{label}" in nodes:
                edges.setdefault((f"ws:{label}", cid), {"source": f"ws:{label}", "target": cid, "type": "label",
                                                         "weight": n})
        newest = sorted(per_collection.get((ws, coll), []), key=lambda d: d.get("addedAt") or "", reverse=True)
        for d in newest[:docs_per_collection]:
            did = f"doc:{d['id']}"
            node(did, d.get("name") or d["id"], "document", workspace=ws, source=g["source"], url=d.get("url"),
                 count=1, chars=int(d.get("chars") or 0))
            edge(cid, did, 1)
    stats = {"documents": sum(g["count"] for g in groups.values()), "workspaces": len(workspaces),
             "sources": len({g["source"] for g in groups.values()}), "collections": len(groups)}
    linked = {x for e in edges.values() for x in (e["source"], e["target"])}
    return {"nodes": [n for n in nodes.values() if n["type"] == "workspace" or n["id"] in linked],
            "edges": list(edges.values()), "stats": stats}


def graph(*, docs_per_collection: int = 3, max_collections: int = 40, refresh: bool = False) -> dict:
    key = (docs_per_collection, max_collections)
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    try:
        try:
            sizes = _get("/api/sizes")  # knowlage's own counts, when it has them
        except (httpx.HTTPError, ValueError):
            sizes = None
        data = build_graph(_get("/api/workspaces"), _get("/api/sources"), _get("/api/documents", timeout=60),
                           docs_per_collection=docs_per_collection, max_collections=max_collections,
                           sizes=sizes if isinstance(sizes, dict) else None)
        out = {"available": True, "url": public_url(), "fetched_at": time.time(), **data}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        if hit:  # stale is better than nothing
            return {**hit[1], "stale": True, "error": str(e)[:200]}
        return {"available": False, "url": public_url(), "error": str(e)[:200], "nodes": [], "edges": [], "stats": {}}
    with _lock:
        _cache[key] = (time.monotonic(), out)
    return out


def ask(question: str, workspace: str | None = None, timeout: float = 300, effort: int | str | None = None) -> dict:
    """Ask knowlage; it streams progress over SSE and ends with the answer
    (markdown with [n] markers and verified citations). `effort` is knowlage's
    level 1–6 (or its name); None = knowlage's default, 3."""
    body = {"question": question, **({"workspace": workspace} if workspace else {}),
            **({"effort": effort} if effort not in (None, "") else {})}
    event, steps, thread = None, 0, None
    try:
        with httpx.stream("POST", base_url() + "/api/ask", json=body, headers=_headers(), timeout=timeout) as r:
            if r.status_code >= 400:
                r.read()
                detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") \
                    else r.text
                return {"ok": False, "error": str(detail)[:500], "status": r.status_code}
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event:
                    data = json.loads(line[5:].strip() or "{}")
                    if event == "thread":
                        thread = (data.get("thread") or {}).get("id")
                    elif event == "progress":
                        steps += 1
                    elif event == "answer":
                        return {"ok": True, "thread_id": thread, "steps": steps, "url": public_url(),
                                "answer": data.get("content", ""), "citations": data.get("citations", []),
                                "verified": data.get("verified"), "problems": data.get("problems", []),
                                "insufficient_evidence": data.get("insufficient_evidence", False),
                                "effort": data.get("effort")}
                    elif event == "error":
                        return {"ok": False, "error": data.get("message", "error"), "limit": data.get("limit", False),
                                "thread_id": thread}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"knowlage is unreachable: {e}"[:300]}
    return {"ok": False, "error": "knowlage ended without an answer", "thread_id": thread}


# ------------------------------------------------------------------ subsystems

_sub_cache: dict = {}


def _probe(url: str) -> tuple[bool, int | None, object]:
    t = time.monotonic()
    try:
        r = httpx.get(url, headers=_headers(), timeout=5)
        ms = int((time.monotonic() - t) * 1000)
        ok = r.status_code < 400
        return ok, ms, (r.json() if ok and r.headers.get("content-type", "").startswith("application/json") else r.status_code)
    except (httpx.HTTPError, ValueError) as e:
        return False, None, str(e)[:120]


def subsystems(conn) -> list[dict]:
    """Live state of what PersonalOS depends on: knowlage, Nexus, the runtimes."""
    from . import engines

    hit = _sub_cache.get("v")
    if hit and time.monotonic() - hit[0] < 30:
        out = hit[1]
    else:
        out = []
        ok, ms, _ = _probe(base_url() + "/api/health")
        stats = _probe(base_url() + "/api/stats")[2] if ok else None
        detail = (f"{stats['documents']:,} documents · {stats['chunks']:,} chunks".replace(",", " ")
                  if isinstance(stats, dict) else "unreachable")
        out.append({"name": "Knowledge base", "proto": "knowlage · A2A", "ok": ok, "value": ms, "unit": "ms",
                    "detail": detail, "url": public_url()})
        nexus = os.environ.get("POS_NEXUS_URL") or os.environ.get("POS_NEXUS_A2A_URL", "").split("/a2a")[0]
        if nexus:
            ok, ms, info = _probe(nexus.rstrip("/") + "/health")
            out.append({"name": "Nexus", "proto": "A2A facade", "ok": ok, "value": ms, "unit": "ms",
                        "detail": "reachable" if ok else f"not reachable ({info})", "url": nexus})
        else:
            out.append({"name": "Nexus", "proto": "A2A facade", "ok": False, "value": None, "unit": "",
                        "detail": "not connected yet (POS_NEXUS_A2A_URL)", "url": None})
        _sub_cache["v"] = (time.monotonic(), out)
    st = engines.status(conn)
    order = st.get("order", ["codex", "claude"])
    free = [e for e in order if (st["codex"]["can_run"] if e == "codex" else not st["claude"]["paused_until"])]
    runtime = {"name": "Agent runtime", "proto": "CLI", "ok": bool(free), "value": None, "unit": "",
               "detail": (f"{free[0]} now" + (f" · {order[0]} limited until "
                                              f"{(st[order[0]].get('paused_until') or '')[:16].replace('T', ' ')}"
                                              if free and free[0] != order[0] else ""))
               if free else "no runtime available", "url": None, "current": free[0] if free else None}
    return [*out, runtime]
