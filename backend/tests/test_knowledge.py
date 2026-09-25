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
