from pos import knowledge

WS = [{"id": "default", "name": "Výchozí", "documents": 3}, {"id": "firma", "name": "Celá firma", "documents": 2}]
SRC = [{"id": "github-obseumeu", "name": "GitHub ObseumEU", "targets": [{"target": "ObseumEU/Nexus"}]}]
DOCS = [
    {"id": "a", "name": "Offer video", "channel": "AlexHormozi", "origin": "file", "workspace": "default",
     "addedAt": "2026-01-02", "status": "indexed", "chars": 900, "url": "https://youtu.be/a"},
    {"id": "b", "name": "Pricing video", "channel": "AlexHormozi", "origin": "file", "workspace": "default",
     "addedAt": "2026-01-03", "status": "indexed", "chars": 500},
    {"id": "c", "name": "Broken", "channel": "AlexHormozi", "origin": "file", "workspace": "default", "status": "error"},
    {"id": "d", "name": "README", "channel": "ObseumEU/Nexus", "origin": "github", "workspace": "firma",
     "source": "github:ObseumEU/Nexus:readme", "addedAt": "2026-01-01", "status": "indexed", "chars": 100},
]


def test_graph_links_workspace_source_collection_and_documents():
    g = knowledge.build_graph(WS, SRC, DOCS, docs_per_collection=1)
    ids = {n["id"]: n for n in g["nodes"]}
    assert ids["col:default:AlexHormozi"]["count"] == 2  # the errored document is left out
    assert ids["ws:default"]["count"] == 2 and ids["src:files"]["count"] == 2  # rolled up
    assert ids["col:firma:ObseumEU/Nexus"]["url"] == "https://github.com/ObseumEU/Nexus"
    assert ids["src:files"]["label"] == "Files and videos"
    assert "doc:b" in ids and "doc:a" not in ids  # the newest per collection
    edges = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("ws:default", "src:files") in edges and ("src:files", "col:default:AlexHormozi") in edges
    assert ("ws:firma", "src:github-obseumeu") in edges and ("col:firma:ObseumEU/Nexus", "doc:d") in edges
    assert g["stats"] == {"documents": 3, "workspaces": 2, "sources": 2, "collections": 2}


def test_graph_degrades_when_knowlage_is_unreachable(monkeypatch):
    monkeypatch.setenv("POS_KNOWLAGE_URL", "http://127.0.0.1:9")
    knowledge._cache.clear()
    g = knowledge.graph(refresh=True)
    assert g["available"] is False and g["nodes"] == [] and g["error"]


def test_graph_reports_knowlages_own_document_total(monkeypatch):
    """One document count everywhere: the graph says what Systém says (knowlage /api/stats)."""
    answers = {"/api/sizes": None, "/api/workspaces": WS, "/api/sources": SRC, "/api/documents": DOCS,
               "/api/stats": {"documents": 33690, "chunks": 128268}}
    monkeypatch.setattr(knowledge, "_get", lambda path, timeout=20: answers[path])
    knowledge._cache.clear()
    g = knowledge.graph(refresh=True)
    assert g["stats"]["documents"] == 33690 and g["stats"]["documents_grouped"] == 3
    knowledge._cache.clear()


def test_sizes_from_knowlage_count_every_collection_even_the_ones_not_shown():
    sizes = {"groups": [{"workspace": "default", "origin": "file", "channel": "AlexHormozi", "documents": 492,
                         "chars": 9000},
                        {"workspace": "default", "origin": "file", "channel": "Other", "documents": 8, "chars": 10},
                        {"workspace": "firma", "origin": "github", "channel": "ObseumEU/Nexus", "documents": 120,
                         "chars": 50}]}
    g = knowledge.build_graph(WS, SRC, DOCS, max_collections=1, sizes=sizes)
    ids = {n["id"]: n for n in g["nodes"]}
    assert ids["col:default:AlexHormozi"]["count"] == 492 and "col:default:Other" not in ids
    assert ids["ws:default"]["count"] == 500 and ids["src:files"]["count"] == 500  # includes the hidden one
    assert ids["src:github-obseumeu"]["count"] == 120 and g["stats"]["documents"] == 620


def test_one_pile_sizes_with_workspaces_as_labels():
    ws = [{"id": "firma", "name": "Celá firma"}, {"id": "hormozi", "name": "Hormozi"}]
    sizes = {"company": "firma", "documents": 620,
             "workspaces": [{"workspace": "firma", "documents": 620}, {"workspace": "hormozi", "documents": 492}],
             "groups": [{"origin": "file", "channel": "AlexHormozi", "documents": 492, "labels": {"hormozi": 492}},
                        {"origin": "github", "channel": "ObseumEU/Nexus", "documents": 128, "labels": {}}]}
    g = knowledge.build_graph(ws, SRC, DOCS, sizes=sizes)
    ids = {n["id"]: n for n in g["nodes"]}
    assert ids["ws:firma"]["count"] == 620 and ids["ws:hormozi"]["count"] == 492
    assert ids["col:firma:AlexHormozi"]["count"] == 492
    kinds = {(e["source"], e["target"]): e["type"] for e in g["edges"]}
    assert kinds[("ws:hormozi", "col:firma:AlexHormozi")] == "label"
    assert kinds[("ws:firma", "src:files")] == "contains"


class _Stream:
    """httpx.stream stand-in: records the request, answers with knowlage's SSE."""

    def __init__(self, calls, effort_level):
        self.calls, self.level = calls, effort_level

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    status_code = 200
    headers = {}

    def iter_lines(self):
        import json as _json

        yield "event: thread"
        yield 'data: {"thread": {"id": "t1"}}'
        yield "event: answer"
        yield "data: " + _json.dumps({"content": "ok", "verified": True, "effort": {"level": self.level}})


def test_ask_passes_the_effort_level_and_returns_its_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(knowledge.httpx, "stream", _Stream(calls, 1))
    out = knowledge.ask("Co s cenou?", effort="blesk")
    assert calls[-1] == {"question": "Co s cenou?", "effort": "blesk"} and out["effort"] == {"level": 1}
    knowledge.ask("Co s cenou?")  # a person's question: knowlage's default (3)
    assert "effort" not in calls[-1]


def test_agents_ask_the_knowledge_agent_cheaply(monkeypatch, tmp_path):
    from pos import a2a, actors
    from pos.core import Ctx
    from pos.db import connect, migrate

    sent = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        def send(self, text, context_id=None, metadata=None):
            sent.append(metadata)
            return {"message": {"parts": [{"text": "answer"}]}}

    conn = connect(tmp_path / "pos.db")
    migrate(conn)
    actors.ensure_builtin(conn)
    conn.execute("UPDATE actors SET a2a_url = ? WHERE name = ?", ("http://kb/a2a", "Knowledge agent"))
    conn.commit()
    monkeypatch.setattr(a2a, "A2AClient", Client)
    me = Ctx(actors.owner_id(conn))
    assert a2a.ask(conn, me, "Knowledge agent", "Q?")["answer"] == "answer"
    a2a.ask(conn, me, "Knowledge agent", "Q?", effort=4)
    assert sent == [{"effort": 2}, {"effort": 4}]
    conn.close()


def test_subsystems_never_waits_on_the_probes_once_cached(monkeypatch):
    """T-464 (p95 alert): a stale cache is served at once and refreshed in the background."""
    import time

    from pos import engines

    calls = []

    def slow_probe(url):
        calls.append(url)
        time.sleep(0.3)
        return True, 300, {"documents": 1, "chunks": 2}

    monkeypatch.setattr(knowledge, "_probe", slow_probe)
    monkeypatch.setattr(engines, "status", lambda conn: {"order": ["codex"], "codex": {"can_run": True},
                                                         "claude": {"paused_until": None}})
    monkeypatch.setenv("POS_NEXUS_URL", "http://nexus")
    monkeypatch.setattr(knowledge, "_sub_cache", {})

    t = time.monotonic()
    first = knowledge.subsystems(None)  # cold: knowlage (2 probes) and Nexus run side by side
    assert time.monotonic() - t < 0.8
    assert [s["name"] for s in first] == ["Knowledge base", "Nexus", "Agent runtime"]

    knowledge._sub_cache["v"] = (time.monotonic() - knowledge.SUB_FRESH_S - 1, first[:2])  # stale
    n = len(calls)
    t = time.monotonic()
    again = knowledge.subsystems(None)
    assert time.monotonic() - t < 0.1 and again[:2] == first[:2]
    assert knowledge._sub_refresh.acquire(timeout=3)  # the background refresh finished
    knowledge._sub_refresh.release()
    assert len(calls) == n + 3 and time.monotonic() - knowledge._sub_cache["v"][0] < 1
