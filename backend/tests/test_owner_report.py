"""The owner's report (pos.owner_report), the reference resolver (pos.refs) and full note reads
and in-place edits (pos.notes): what the owner sees must be self-contained and never invented."""

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, chat, kb_files, mcp_server, notes, owner_report, refs, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate

# ------------------------------------------------------------------ the owner's example (T-434, 1. 10.)

# The hand-in as it reached the owner (the parts quoted in his complaint), with what it points at.
EXAMPLE_RESULT = """## Hormozi kontrola poznámek 20 a 21
**Proč:** pokyn ownera (#obseum-ai msg {owner_msg}): vycházet z Hormoziho v každém kroku.

**Co je hotové**
- Poznámka **{n23}** obsahuje kontrolu podle hodnotové rovnice, Grand Slam Offer (7 prvků) a Core Four / lead getters. U každé části je citace z KB: `firma.gh_0ff08113fa5428ee:c13`, `k834E6OTGTM:c17`, `kniha.gh_27a60d9ca94e73c3:c0/c2`.
- CEO dostal v DM (msg {dm}) návrhy k rozhodnutí: garance, bonusy, nedostatek a naléhavost, název, odměna partnerů.
- Shrnutí je v #obseum-ai (msg {summary}).

**Odchylka od zadání:** kontrolu jsem zapsal do samostatné poznámky {n23}, ne do poznámek {n20} a {n21}. Celé texty {n20} a {n21} v tomto běhu nemám.

**Ke kontrole:** zda samostatná poznámka stačí jako splnění zadání a zda jsou návrhy garance a odměny partnerů rozumné. Result: note:{n23}"""

NOTE_23 = """# Obseum AI: kontrola podle Hormoziho

## Grand Slam Offer (7 prvků)
| Prvek | Stav | Návrh (pro CEO) |
|---|---|---|
| Bonusy | žádné | školení týmu kanceláře (1–2 h), šablona kontrol DPH, 30 dní podpory |
| Garance | žádná | když fáze 1 neušetří dohodnutý počet hodin, dopracujeme ji bez příplatku |
| Název | „Obseum AI“ | AI asistent pro účetní kanceláře: X hodin měsíčně zpět do 30 dní |

## Core Four
- Odměna partnerů 15 % + 10 % může být „20 %, které nikoho nezajímá“."""

DM_PROPOSALS = """T-{ref}: Hormozi kontrola plánu (pozn. {n20}) a sales kitu ({n21}) je v poznámce **{n23}** (citace KB). Návrhy k tvému rozhodnutí (nic nejde ven bez schválení):
1. **Garance** (rozhoduje owner): „když fáze 1 neušetří dohodnutý počet hodin, dopracujeme bez příplatku“.
2. **Bonusy**: školení týmu kanceláře 1–2 h, šablona kontrol DPH/vytěžování, 30 dní podpory, každý s hodnotou v Kč.
3. **Nedostatek / naléhavost**: max. 3 kanceláře do konce roku (podle kapacity Davida), spuštění před uzávěrkou roku.
4. **Název**: „AI asistent pro účetní kanceláře: X hodin měsíčně zpět do 30 dní“ (X dodá David).
5. **Odměna partnerů**: 15 % + 10 % může být málo („20 % nikoho nezajímá“). Hledat partnery, kteří účetní kanceláře už mají jako klienty, a dát jim silnější podíl z první zakázky.
Pořadí podle Hormoziho: nejdřív cena a jádro fáze 1 (David do 6. 10.), potom bonusy a garance."""

DOC_K834 = """<external source="file" trust="untrusted" ref="k834E6OTGTM">
# $100M Leads Launch
id: `k834E6OTGTM` · date: 2023-08-19 · channel: AlexHormozi · url: https://youtu.be/k834E6OTGTM

## `k834E6OTGTM:s5` · Part 6 (30:00–35:00)

[k834E6OTGTM:c16 · 31:10] [Alex Hormozi] Affiliates do the core four to their audience on your behalf.

[k834E6OTGTM:c17 · 33:39] [Alex Hormozi] Over 104,361 of you are here from affiliates. And that's how you can know this affiliate stuff works.

[k834E6OTGTM:c18 · 35:02] [Alex Hormozi] Next part.
</external>"""


def _conn(tmp_path):
    c = connect(tmp_path / "r.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return c


def _agent(conn, owner, name, perms=("tasks:read", "tasks:claim", "tasks:write", "messages:send",
                                     "approvals:request", "tasks:review")):
    from pathlib import Path

    data = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", permissions=list(perms),
                               data_dir=data)
    return made["agent"]["id"]


def seed_example(conn) -> dict:
    """The owner's example, reconstructed: notes 20, 21, 23, the owner's message, the proposals in the
    CEO's DM, the summary in #obseum-ai and the task in review with the free-form result."""
    owner = Ctx(actors.owner_id(conn))
    ceo = _agent(conn, owner, "CEO")
    hog = _agent(conn, owner, "Head of Growth")
    n20 = notes.create(conn, owner, {"title": "Obseum AI: plán obchodu a náboru obchodníků",
                                     "body": "## Plán\nSegment účetní kanceláře, warm outreach a doporučení."})
    n21 = notes.create(conn, owner, {"title": "Obseum AI: sales kit v1", "body": "## Sales kit\nDemo na datech klienta."})
    n23 = notes.create(conn, owner, {"title": "Obseum AI: kontrola plánu (20) a sales kitu (21) podle Hormoziho",
                                     "body": NOTE_23})
    room = chat.create_channel(conn, Ctx(ceo), "obseum-ai", members=["Head of Growth"])
    om = chat.send(conn, owner, room["id"], "Hlavně berte knowlage z Hormoziho, inspirujte se tím v každém kroku!")
    t = tasks.create(conn, Ctx(ceo), {"title": "Obseum AI: ověřit plán obchodu a sales kit proti Hormozimu",
                                      "assignee": "Head of Growth", "priority": 2, "deadline": "2026-10-06",
                                      "reviewer": "Owner", "notes": "Projít poznámku 20 a 21 proti Hormozimu."})
    ids = {"n20": n20["id"], "n21": n21["id"], "n23": n23["id"], "ref": t["id"], "owner_msg": om["id"]}
    dm = chat.send_dm(conn, Ctx(hog), ceo, DM_PROPOSALS.format(**ids))
    sm = chat.send(conn, Ctx(hog), room["id"], f"Hormozi kontrola je hotová, viz poznámka **{n23['id']}**.")
    ids.update(dm=dm["id"], summary=sm["id"])
    tasks.claim(conn, Ctx(hog, via="mcp"), t["id"])
    tasks.request_review(conn, Ctx(hog, via="mcp"), t["id"], "Owner", EXAMPLE_RESULT.format(**ids))
    conn.commit()
    return {**ids, "task": t["id"], "ref_text": t["ref"], "ceo": ceo, "hog": hog, "owner": owner}


# ------------------------------------------------------------------ the report schema

GOOD = {
    "takeaway": "Plán obchodu je v zásadě dobrý. Chybí garance a naléhavost. Rozhodni garanci, zbytek dotáhneme.",
    "decisions": [{"question": "Nabídneme garanci?", "options": ["Ano, podmíněnou", "Ne"],
                   "recommendation": "Ano, podmíněnou", "why": "Zvyšuje důvěru."}],
    "next": "Po rozhodnutí doplníme plán a sales kit.",
    "content": "## Kontrola\n" + "| Prvek | Stav |\n|---|---|\n| Garance | chybí |\n" * 30,
    "sources": [{"title": "$100M Leads Launch", "quote": "Affiliates do the core four", "link": "https://youtu.be/x"}],
}


def test_a_good_report_passes_and_is_normalized():
    rep = owner_report.validate(GOOD)
    assert rep["decisions"][0]["id"].startswith("d") and rep["summary"] == []
    assert owner_report.validate(json.dumps(GOOD))["takeaway"] == GOOD["takeaway"]


@pytest.mark.parametrize("change, problem", [
    ({"content": "Výsledek je v poznámce 23."}, "only points elsewhere"),
    ({"content": ""}, "content is missing"),
    ({"takeaway": ""}, "takeaway is missing"),
    ({"takeaway": "Viz `k834E6OTGTM:c17` a msg 1095."}, "raw ids"),
    ({"takeaway": "Jedna. Dvě. Tři. Čtyři."}, "too long"),
    ({"decisions": [{"question": f"Q{i}", "options": ["a", "b"]} for i in range(4)]}, "at most 3"),
    ({"decisions": [{"question": "Q", "options": ["jen jedna"]}]}, "at least 2 options"),
    ({"decisions": [{"question": "Q", "options": ["a", "b"], "recommendation": "c"}]}, "one of its options"),
    ({"summary": ["a", "b", "c", "d"]}, "at most 3"),
    ({"sources": [{"title": "k834E6OTGTM:c17"}]}, "raw chunk id"),
])
def test_a_report_that_is_not_self_contained_is_refused(change, problem):
    with pytest.raises(tasks.Invalid) as e:
        owner_report.validate({**GOOD, **change})
    assert problem in str(e.value)


# ------------------------------------------------------------------ the reference resolver

@pytest.mark.parametrize("text, expected", [
    ("Result: note:23", [("note", "23")]),
    ("Poznámka **23** obsahuje kontrolu", [("note", "23")]),
    ("ne do poznámek 20 a 21", [("note", "20"), ("note", "21")]),
    ("plánu (pozn. 20) a", [("note", "20")]),
    ("hotové v note #6.", [("note", "6")]),
    ("CEO dostal v DM (msg 1095) návrhy", [("msg", "1095")]),
    ("(#obseum-ai msg 1088)", [("msg", "1088")]),
    ("kanál 37, zpráva 504", [("msg", "504")]),
    ("`firma.gh_0ff08113fa5428ee:c13`", [("chunk", "firma.gh_0ff08113fa5428ee:c13")]),
    ("`k834E6OTGTM:c17`", [("chunk", "k834E6OTGTM:c17")]),
    ("`kniha.gh_27a60d9ca94e73c3:c0/c2`", [("chunk", "kniha.gh_27a60d9ca94e73c3:c0"),
                                          ("chunk", "kniha.gh_27a60d9ca94e73c3:c2")]),
    ("(XwZH-lOKG9c:c17, ryNgeWQSVuo:c0)", [("chunk", "XwZH-lOKG9c:c17"), ("chunk", "ryNgeWQSVuo:c0")]),
    ("navazuje na T-431.", [("task", "T-431")]),
    ("viz file:12", [("file", "12")]),
    ("v 18:32, https://example.com/a:c12 a zprávy 3 dny", []),
])
def test_extract_finds_each_reference_pattern(text, expected):
    assert [(r["kind"], r["id"]) for r in refs.extract(text)] == expected


def test_resolve_note_message_task_file_and_chunk(tmp_path, monkeypatch):
    import io

    from pos import files

    conn = _conn(tmp_path)
    ex = seed_example(conn)
    owner = ex["owner"]
    n = refs.resolve(conn, owner, "note", str(ex["n23"]))
    assert n["ok"] and n["text"] == NOTE_23 and n["link"] == f"/notes?note={ex['n23']}"
    m = refs.resolve(conn, owner, "msg", str(ex["dm"]))
    assert m["ok"] and "Garance" in m["text"] and m["channel"].startswith("Soukromá zpráva")
    assert m["author"] == "Head of Growth"
    s = refs.resolve(conn, owner, "msg", str(ex["summary"]))
    assert s["channel"] == "#obseum-ai" and [c["id"] for c in s["context"]] == [ex["owner_msg"]]
    t = refs.resolve(conn, owner, "task", "T-431")
    assert not t["ok"] and t["error"] == "neexistuje"
    t = refs.resolve(conn, owner, "task", ex["ref_text"])
    assert t["ok"] and t["title"].startswith("Obseum AI") and t["link"] == f"/tasks/{ex['ref_text']}"
    f = files.upload(conn, owner, tmp_path / "files", io.BytesIO(b"Sales kit text"), "kit.txt")
    got = refs.resolve(conn, owner, "file", str(f["id"]))
    assert got["ok"] and got["title"] == "kit.txt" and "Sales kit" in got["text"]
    # A member who may not read the CEO's DM gets no preview of it.
    other = _agent(conn, owner, "Outsider", perms=("tasks:read",))
    assert refs.resolve(conn, Ctx(other), "msg", str(ex["dm"]))["error"] == "nemáš k tomu přístup"

    # Chunks: knowlage read_document, the labelled paragraph, a timed link.
    assert refs.resolve(conn, owner, "chunk", "k834E6OTGTM:c17")["ok"] is False  # not configured here
    monkeypatch.setattr(kb_files, "configured", lambda: True)
    seen = []
    monkeypatch.setattr(refs, "_read_document", lambda d: seen.append(d) or DOC_K834)
    c = refs.resolve(conn, owner, "chunk", "k834E6OTGTM:c17")
    assert c["ok"] and c["title"] == "$100M Leads Launch"
    assert c["quote"].startswith("[Alex Hormozi] Over 104,361") and "Next part" not in c["quote"]
    assert c["link"] == "https://youtu.be/k834E6OTGTM?t=2019"
    assert refs.resolve(conn, owner, "chunk", "k834E6OTGTM:c99")["error"] == "pasáž v dokumentu nenalezena"


def test_api_ref_preview(tmp_path):
    from pos.main import create_app

    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        ex = seed_example(conn)
        conn.close()
        assert client.get(f"/api/refs/note:{ex['n23']}").json()["title"].startswith("Obseum AI: kontrola")
        assert client.get(f"/api/refs/msg:{ex['dm']}").json()["ok"]
        assert client.get("/api/refs/bogus:1").status_code == 404


# ------------------------------------------------------------------ the builder

def _no_llm(monkeypatch):
    monkeypatch.setattr(owner_report, "_llm", lambda conn, task, material, hint: None)


def test_example_becomes_a_takeaway_first_report_without_a_model(tmp_path, monkeypatch):
    """The owner's example: the takeaway first, at most three decisions (the five proposals from the
    CEO's DM), the next step, and the details (note 23 inline, the sources, what was not found)."""
    _no_llm(monkeypatch)
    conn = _conn(tmp_path)
    ex = seed_example(conn)
    view = owner_report.current(conn, ex["owner"], ex["task"])
    assert list(view)[:0] == [] and view["available"] and view["source"] == "builder"
    keys = list(view)
    assert keys.index("takeaway") < keys.index("decisions") < keys.index("next") < keys.index("details")
    assert view["takeaway"] and "msg" not in view["takeaway"]
    qs = [d["question"] for d in view["decisions"]]
    assert len(qs) == 3 and qs[0].startswith("Garance") and qs[1].startswith("Bonusy")
    assert "Nedostatek / naléhavost" in qs[2] and "Název" in qs[2] and "Odměna partnerů" in qs[2]
    assert all(len(d["options"]) >= 2 and d["recommendation"] in d["options"] for d in view["decisions"])
    assert view["next"]
    d = view["details"]
    assert d["content"] == NOTE_23  # the note itself, inline (its title is content_title)
    assert d["content_title"].startswith("Obseum AI: kontrola")
    related = {(r["kind"], str(r["id"])) for r in d["related"]}
    assert {("note", str(ex["n20"])), ("note", str(ex["n21"])), ("msg", str(ex["dm"]))} <= related
    assert {u["id"] for u in d["unresolved"]} >= {"k834E6OTGTM:c17", "firma.gh_0ff08113fa5428ee:c13"}
    assert d["original"].startswith("## Hormozi kontrola")
    # Cached per task version: no second build until the result changes.
    calls = []
    monkeypatch.setattr(owner_report, "build", lambda *a: calls.append(1) or (_ for _ in ()).throw(AssertionError))
    assert owner_report.current(conn, ex["owner"], ex["task"])["takeaway"] == view["takeaway"]
    assert calls == []


def test_the_builder_never_keeps_what_the_material_does_not_say(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    ex = seed_example(conn)
    invented = {
        "takeaway": "Tržby vzrostou o 47 % během 9 měsíců díky nové reklamní kampani na Facebooku.",
        "next": "Spustíme reklamu za 50 000 Kč.",
        "summary": ["Konkurence snížila ceny o 12 %."],
        "decisions": [
            {"question": "Schválíš rozpočet 50 000 Kč na reklamu?", "options": ["Ano", "Ne"],
             "recommendation": "Ano", "why": "Reklama přinese leady.", "evidence": "reklama přinese leady"},
            {"question": "Nabídneme garanci, že když fáze 1 neušetří hodiny, dopracujeme ji bez příplatku?",
             "options": ["Ano, souhlasím", "Ne", "Ne, stačí vrácení peněz do 30 dní"], "recommendation": "Ano, souhlasím",
             "why": "Konkurenti ji mají taky.",
             "evidence": "když fáze 1 neušetří dohodnutý počet hodin, dopracujeme bez příplatku"},
        ],
    }
    monkeypatch.setattr(owner_report, "_llm", lambda conn, task, material, hint: (invented, 7))
    view = owner_report.current(conn, ex["owner"], ex["task"])
    # Only the decision quoted from the material stays; everything invented is dropped and listed.
    # The agent's other proposals the model did not cover are added in the agent's own words.
    qs = [d["question"] for d in view["decisions"]]
    assert qs[0] == invented["decisions"][1]["question"] and len(qs) == 3
    assert not any("rozpočet" in q or "50 000" in q for q in qs)
    assert qs[1].startswith("Bonusy") and "Odměna partnerů" in qs[2]
    assert view["decisions"][1]["options"] == list(owner_report.YES_NO_OTHER)
    assert "47" not in view["takeaway"] and "Facebook" not in view["takeaway"]
    assert view["takeaway_source"] == "fallback" and view["details"]["summary"] == []
    assert "50 000" not in view["next"]
    dropped = " ".join(view["details"]["dropped"])
    assert "takeaway" in dropped and "rozpočet" in dropped and "Konkurence" in dropped
    assert "vrácení peněz" in dropped and "Konkurenti" in dropped
    assert view["decisions"][0]["options"] == ["Ano, souhlasím", "Ne"] and view["decisions"][0]["why"] == ""
    # The content is the resolved note itself, never model text.
    assert NOTE_23 in view["details"]["content"]


def test_a_grounded_model_answer_is_kept(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    ex = seed_example(conn)
    good = {
        "takeaway": "Kontrola plánu obchodu je hotová. Nabídce chybí garance a bonusy. Potřebuju od tebe "
                    "rozhodnout garanci a odměnu partnerů.",
        "next": "Po tvém rozhodnutí se návrhy doplní do plánu a sales kitu.",
        "summary": ["Odměna partnerů 15 % + 10 % může být málo."],
        "decisions": [{"question": "Odměna partnerů: dát partnerům silnější podíl z první zakázky?",
                       "options": ["Ano, silnější podíl", "Ne, nechat 15 % + 10 %"],
                       "recommendation": "Ano, silnější podíl", "why": "15 % + 10 % může být málo.",
                       "evidence": "15 % + 10 % může být málo"}],
    }
    monkeypatch.setattr(owner_report, "_llm", lambda conn, task, material, hint: (good, 7))
    view = owner_report.current(conn, ex["owner"], ex["task"])
    assert view["takeaway"] == good["takeaway"] and view["takeaway_source"] == "llm"
    assert view["next"] == good["next"] and view["details"]["summary"] == good["summary"]
    qs = [d["question"] for d in view["decisions"]]
    assert qs[0] == good["decisions"][0]["question"] and len(qs) == 3
    assert qs[1].startswith("Garance") and "Bonusy" in qs[2] and "Název" in qs[2] and "Odměna" not in qs[2]
    assert view["decisions"][0]["recommendation"] == "Ano, silnější podíl"
    assert view["decisions"][0]["options"] == ["Ano, silnější podíl", "Ne, nechat 15 % + 10 %"]


# ------------------------------------------------------------------ deciding

def test_deciding_records_on_the_task_and_the_last_answer_resumes_the_agent(tmp_path, monkeypatch):
    _no_llm(monkeypatch)
    conn = _conn(tmp_path)
    ex = seed_example(conn)
    owner = ex["owner"]
    view = owner_report.current(conn, owner, ex["task"])
    first, *rest = view["decisions"]
    out = owner_report.decide(conn, owner, ex["task"], first["id"], first["options"][0], "ale jen na fázi 1")
    assert not out["resumed"] and out["decisions"][0]["decided"]["choice"] == first["options"][0]
    assert tasks.get(conn, owner, ex["task"])["status"] == "review"
    with pytest.raises(tasks.Invalid):
        owner_report.decide(conn, owner, ex["task"], rest[0]["id"], "něco jiného")
    with pytest.raises(tasks.Invalid):
        owner_report.decide(conn, owner, ex["task"], rest[0]["id"])
    for d in rest[:-1]:
        owner_report.decide(conn, owner, ex["task"], d["id"], d["options"][1])
    out = owner_report.decide(conn, owner, ex["task"], rest[-1]["id"], "", "Až po ceně fáze 1.")
    assert out["resumed"] and out["decisions_open"] == 0
    t = tasks.get(conn, owner, ex["task"])
    assert t["status"] == "next" and t["assignee_name"] == "Head of Growth"
    dm = conn.execute("""SELECT m.body FROM chat_messages m JOIN channels c ON c.id = m.channel_id
                         WHERE c.kind = 'dm' AND m.author_id = ? ORDER BY m.id DESC LIMIT 1""",
                      (owner.actor_id,)).fetchone()["body"]
    assert "ale jen na fázi 1" in dm and "Až po ceně fáze 1." in dm and "note_update" in dm
    log = [r["body"] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (ex["task"],))]
    assert sum(b.startswith("**Rozhodnutí:**") for b in log) == len(view["decisions"])
    # The report stays visible with the decisions after the task moved on.
    again = owner_report.current(conn, owner, ex["task"])
    assert again["available"] and all(d["decided"] for d in again["decisions"])
    # Someone who is not the decider may not decide.
    with pytest.raises(Exception):
        owner_report.decide(conn, Ctx(_agent(conn, owner, "Bystander", perms=("tasks:read",))), ex["task"],
                            first["id"], first["options"][0])


def test_report_api_and_decisions_over_http(tmp_path, monkeypatch):
    from pos.main import create_app

    _no_llm(monkeypatch)
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        ex = seed_example(conn)
        conn.close()
        ref = ex["ref_text"]
        assert client.get(f"/api/tasks/{ref}/report?generate=false").json() == {
            "available": False, "task": ref, "can_build": True}
        view = client.get(f"/api/tasks/{ref}/report").json()
        assert view["available"] and len(view["decisions"]) == 3 and view["report_url"] == f"/report/{ref}"
        d = view["decisions"][0]
        r = client.post(f"/api/tasks/{ref}/report/decisions", json={"decision": d["id"], "choice": d["options"][0]})
        assert r.status_code == 200 and r.json()["decisions"][0]["decided"]["choice"] == d["options"][0]
        assert client.post(f"/api/tasks/{ref}/report/decisions", json={"decision": "nope", "choice": "x"}).status_code == 404
        # The task's Shrnutí and the board show the takeaway.
        assert client.get(f"/api/tasks/{ref}/summary").json()["text"] == view["takeaway"]
        board = client.get("/api/tasks?view=board").json()
        assert next(t for t in board if t["ref"] == ref)["summary"] == view["takeaway"]


def test_an_agents_report_on_hand_in_is_validated_and_stored(tmp_path):
    db = tmp_path / "m.db"
    conn = connect(db)
    migrate(conn)
    actors.ensure_builtin(conn)
    owner = Ctx(actors.owner_id(conn))
    hog = _agent(conn, owner, "Head of Growth")
    t = tasks.create(conn, owner, {"title": "Plan check", "assignee": "Head of Growth", "status": "next"})
    tasks.claim(conn, Ctx(hog, via="mcp"), t["id"])
    conn.commit()
    server = mcp_server.build(db, default_actor=lambda c: hog)

    def call(result):
        assert not result.is_error, result.content
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else json.loads(result.content[0].text)

    async def scenario():
        async with Client(server) as c:
            bad = await c.call_tool("request_review", {"task_id": t["ref"], "reviewer": "Owner",
                                                       "report": {**GOOD, "content": "Viz poznámka 23."}})
            assert bad.is_error and "only points elsewhere" in bad.content[0].text
            ok = call(await c.call_tool("request_review", {"task_id": t["ref"], "reviewer": "Owner", "report": GOOD}))
            assert ok["status"] == "review" and ok["report"]["decisions"] == 1

    anyio.run(scenario)
    view = owner_report.current(conn, owner, t["id"])
    assert view["source"] == "agent" and view["author"] == "Head of Growth"
    assert view["takeaway"] == GOOD["takeaway"] and view["details"]["content"] == GOOD["content"].strip()
    assert tasks.get(conn, owner, t["id"])["status"] == "review"


# ------------------------------------------------------------------ notes: full reads, edits in place

LONG = "# Plán\n\n## Cena\nZatím chybí.\n\n## Rozhodnutí\n| Rozhodnutí | Ověření |\n|---|---|\n| A | B |\n\n## Dodatek\n" + "x" * 45_000


def test_note_read_is_paged_and_complete(tmp_path):
    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    n = notes.create(conn, me, {"title": "Plán", "body": LONG})
    first = notes.read(conn, me, n["id"])
    assert len(first["body"]) == notes.PAGE and first["next_offset"] == notes.PAGE and not first["complete"]
    assert [h["title"] for h in first["outline"]] == ["Plán", "Cena", "Rozhodnutí", "Dodatek"]
    body, offset = "", 0
    while offset is not None:
        page = notes.read(conn, me, n["id"], offset)
        body += page["body"]
        offset = page["next_offset"]
    assert body == LONG and page["total_chars"] == len(LONG)
    assert notes.read(conn, me, n["id"], 0, 100_000)["complete"]


def test_note_edit_section_patch_append(tmp_path):
    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    n = notes.create(conn, me, {"title": "Plán", "body": LONG})
    out = notes.edit(conn, me, n["id"], "section", section="## Cena", body="Fáze 1: 49 000 Kč.")
    assert "## Cena\n\nFáze 1: 49 000 Kč.\n\n## Rozhodnutí" in out["body"] and "Zatím chybí" not in out["body"]
    out = notes.edit(conn, me, n["id"], "patch", find="| A | B |", replace="| A | B |\n| Garance | ověřeno |")
    assert "| Garance | ověřeno |" in out["body"] and out["body"].endswith("x" * 100)
    with pytest.raises(tasks.Invalid, match="exactly once"):
        notes.edit(conn, me, n["id"], "patch", find="x", replace="y")
    out = notes.edit(conn, me, n["id"], "section", section="Ověření Hormozi", body="- OK")
    assert out["body"].endswith("## Ověření Hormozi\n\n- OK\n")
    out = notes.edit(conn, me, n["id"], "append", body="Konec.")
    assert out["body"].endswith("- OK\n\nKonec.\n")
    with pytest.raises(tasks.Invalid):
        notes.edit(conn, me, n["id"], "bogus", body="x")
    assert len(notes.history(conn, me, n["id"])) >= 5  # every edit is versioned


def test_note_tools_over_mcp(tmp_path):
    db = tmp_path / "n.db"
    conn = connect(db)
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    agents.seed_builtin_permissions(conn)
    me = Ctx(actors.owner_id(conn))
    n = notes.create(conn, me, {"title": "Plán", "body": LONG})
    conn.commit()
    server = mcp_server.build(db, default_actor=lambda c: ids["Nexus"])

    def call(result):
        assert not result.is_error, result.content
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else json.loads(result.content[0].text)

    async def scenario():
        async with Client(server) as c:
            got = call(await c.call_tool("note_get", {"note_id": n["id"], "limit": 50_000}))
            assert got["total_chars"] == len(LONG) and got["next_offset"] is None and got["complete"]
            out = call(await c.call_tool("note_update", {"note_id": n["id"], "mode": "section",
                                                         "section": "Cena", "body": "Fáze 1: 49 000 Kč."}))
            assert any(h["title"] == "Cena" for h in out["outline"]) and "49 000" in out["body"]
            out = call(await c.call_tool("note_update", {"note_id": n["id"], "mode": "patch", "find": "| A | B |",
                                                         "replace": "| A | ověřeno |"}))
            assert "| A | ověřeno |" in out["body"]
            assert (await c.call_tool("note_update", {"note_id": n["id"]})).is_error

    anyio.run(scenario)


def test_no_proposal_is_lost_when_the_model_fills_all_three_slots(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    ex = seed_example(conn)

    def dec(q, ev):
        return {"question": q, "options": ["Ano, souhlasím", "Ne"], "evidence": ev}

    out = {"takeaway": "", "next": "", "decisions": [
        dec("Garance: dopracujeme bez příplatku?", "neušetří dohodnutý počet hodin, dopracujeme bez příplatku"),
        dec("Bonusy: školení týmu kanceláře a 30 dní podpory?", "školení týmu kanceláře 1–2 h"),
        dec("Odměna partnerů: silnější podíl z první zakázky?", "dát jim silnější podíl z první zakázky")]}
    monkeypatch.setattr(owner_report, "_llm", lambda conn, task, material, hint: (out, 7))
    qs = [d["question"] for d in owner_report.current(conn, ex["owner"], ex["task"])["decisions"]]
    assert len(qs) == 3 and qs[0].startswith("Garance") and qs[1].startswith("Bonusy")
    assert all(w in qs[2] for w in ("Nedostatek", "Název", "Odměna partnerů"))
