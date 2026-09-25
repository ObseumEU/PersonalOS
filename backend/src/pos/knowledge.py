"""Our knowledge base (knowlage-agent) as seen from PersonalOS: the graph of its
workspaces, sources, collections and documents, and questions answered with
verified citations.

knowlage has no graph API of its own, so the graph is built here from its read
API (/api/workspaces, /api/sources, /api/documents) and cached for a few
minutes. When knowlage is unreachable the screens get `available: false` and
show that, instead of breaking.

Settings: POS_KNOWLAGE_URL (default https://knowlage.obseum.cz),
POS_KNOWLAGE_API_KEY when knowlage runs with KB_API_KEY.
"""

import json
import os
import threading
import time
from collections import Counter, defaultdict

import httpx

CACHE_S = 300
_cache: dict = {}
_lock = threading.Lock()


def base_url() -> str:
    return os.environ.get("POS_KNOWLAGE_URL", "https://knowlage.obseum.cz").rstrip("/")


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
                docs_per_collection: int = 3, max_collections: int = 40) -> dict:
    """Nodes {id, label, type, workspace, source, url, weight} and edges
    {source, target, type, weight}: workspace → source → collection → document."""
    docs = [d for d in documents if d.get("status") != "error" and d.get("workspace")]
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(nid: str, label: str, kind: str, **kw) -> dict:
        n = nodes.setdefault(nid, {"id": nid, "label": label, "type": kind, "workspace": kw.get("workspace"),
                                   "source": kw.get("source"), "url": kw.get("url"), "weight": 0})
        n["weight"] += kw.get("weight", 0)
        return n

    for w in workspaces:
        node(f"ws:{w['id']}", w.get("name") or w["id"], "workspace", workspace=w["id"], weight=w.get("documents", 0))
    # Sources: connectors (GitHub, ...) by id; uploaded files and videos are "files".
    repo_source = {t["target"]: s["id"] for s in sources for t in s.get("targets", [])}
    for s in sources:
        node(f"src:{s['id']}", s.get("name") or s["id"], "source", source=s["id"])
    per_collection: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in docs:
        per_collection[(d["workspace"], _collection(d))].append(d)
    top = Counter({k: len(v) for k, v in per_collection.items()}).most_common(max_collections)
    for (ws, coll), count in top:
        src_id = repo_source.get(coll) or ("files" if all(d.get("origin") == "file" for d in per_collection[(ws, coll)])
                                           else per_collection[(ws, coll)][0].get("origin") or "files")
        src = f"src:{src_id}"
        if src not in nodes:
            node(src, "Files and videos" if src_id == "files" else src_id, "source", source=src_id)
        cid = f"col:{ws}:{coll}"
        node(cid, coll, "collection", workspace=ws, source=src_id, weight=count,
             url=f"https://github.com/{coll}" if src_id != "files" and "/" in coll else None)
        nodes[src]["weight"] += count
        edges.append({"source": f"ws:{ws}", "target": src, "type": "contains", "weight": count})
        edges.append({"source": src, "target": cid, "type": "contains", "weight": count})
        newest = sorted(per_collection[(ws, coll)], key=lambda d: d.get("addedAt") or "", reverse=True)
        for d in newest[:docs_per_collection]:
            did = f"doc:{d['id']}"
            node(did, d.get("name") or d["id"], "document", workspace=ws, source=src_id, url=d.get("url"),
                 weight=int(d.get("chars") or 0))
            edges.append({"source": cid, "target": did, "type": "contains", "weight": 1})
    # One ws→source edge per pair, weights summed.
    merged: dict[tuple, dict] = {}
    for e in edges:
        k = (e["source"], e["target"])
        if k in merged:
            merged[k]["weight"] += e["weight"]
        else:
            merged[k] = dict(e)
    stats = {"documents": len(docs), "workspaces": len(workspaces), "sources": len(sources),
             "collections": len(per_collection)}
    linked = {x for e in merged.values() for x in (e["source"], e["target"])}
    return {"nodes": [n for n in nodes.values() if n["type"] == "workspace" or n["id"] in linked],
            "edges": list(merged.values()), "stats": stats}


def graph(*, docs_per_collection: int = 3, max_collections: int = 40, refresh: bool = False) -> dict:
    key = (docs_per_collection, max_collections)
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    try:
        data = build_graph(_get("/api/workspaces"), _get("/api/sources"), _get("/api/documents", timeout=60),
                           docs_per_collection=docs_per_collection, max_collections=max_collections)
        out = {"available": True, "url": base_url(), "fetched_at": time.time(), **data}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        if hit:  # stale is better than nothing
            return {**hit[1], "stale": True, "error": str(e)[:200]}
        return {"available": False, "url": base_url(), "error": str(e)[:200], "nodes": [], "edges": [], "stats": {}}
    with _lock:
        _cache[key] = (time.monotonic(), out)
    return out


def ask(question: str, workspace: str | None = None, timeout: float = 300) -> dict:
    """Ask knowlage; it streams progress over SSE and ends with the answer
    (markdown with [n] markers and verified citations)."""
    body = {"question": question, **({"workspace": workspace} if workspace else {})}
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
                        return {"ok": True, "thread_id": thread, "steps": steps, "url": base_url(),
                                "answer": data.get("content", ""), "citations": data.get("citations", []),
                                "verified": data.get("verified"), "problems": data.get("problems", []),
                                "insufficient_evidence": data.get("insufficient_evidence", False)}
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
                    "detail": detail, "url": base_url()})
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
