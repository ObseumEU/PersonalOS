"""Cheaper reviews (pos.review_packet, pos.review_policy.ceo_offload) and the prompt-cache TTL per agent
(pos.cache_policy, pos_worker cache_env / run_size)."""

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from pos import (actors, agents, api_worker, business, cache_policy, integrations, review_packet, review_work,
                 tasks)
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "r.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role, lead=None, review=True) -> Ctx:
    perms = ["tasks:read", "tasks:write", "tasks:claim", "messages:send", "approvals:request"] \
        + (["tasks:review"] if review else [])
    found = actors.find_by_name(conn, name)
    if found is not None:
        aid = found["id"]
        agents.set_permissions(conn, owner, aid, perms)
    else:
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=perms)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ? WHERE id = ?", (role, lead.actor_id if lead else None, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


@pytest.fixture
def co(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    coo = _agent(conn, owner, tmp_path, "COO", "project_manager", ceo)
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo)
    qa = _agent(conn, owner, tmp_path, "QA Reviewer", "qa", cto)
    se = _agent(conn, owner, tmp_path, "Software Engineer", "developer", cto, review=False)
    cfo = _agent(conn, owner, tmp_path, "CFO", "cfo", ceo, review=False)
    writer = _agent(conn, owner, tmp_path, "Writer", "content", ceo, review=False)
    return {"ceo": ceo, "coo": coo, "cto": cto, "qa": qa, "se": se, "cfo": cfo, "writer": writer}


def _hand_in(conn, creator: Ctx, worker: Ctx, title: str, note: str, **kw) -> dict:
    t = tasks.create(conn, creator, {"title": title, "assignee": {"type": "agent", "id": worker.actor_id}, **kw})
    tasks.claim(conn, worker, t["id"])
    out = tasks.complete(conn, worker, t["id"], note)
    conn.commit()
    return out


def _item(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE source = ? AND status != 'done'", (f"review:{task_id}",)).fetchone()


# ------------------------------------------------------------------ who reviews

def test_the_ceo_reviews_only_owner_facing_and_strategic_results(conn, owner, co):
    ceo, coo = co["ceo"], co["coo"]
    # The CEO delegated it (so it would be the default reviewer): an ordinary analysis goes to the COO.
    out = _hand_in(conn, ceo, co["cfo"], "Analýza vytížení serverů",
                   "Vytížení CPU 40 %, disk 60 %, rezerva na 6 měsíců.\nOvěřeno: grafy za 30 dní.")
    assert out["status"] == "review" and out["reviewer_id"] == coo.actor_id
    assert _item(conn, out["id"])["assignee_id"] == coo.actor_id
    # A plan and a result asking for a decision stay with the CEO.
    out = _hand_in(conn, ceo, co["cfo"], "Plán rozpočtu na Q4", "Tři varianty, doporučuji B.\nOvěřeno: čísla z faktur.")
    assert out["reviewer_id"] == ceo.actor_id
    out = _hand_in(conn, ceo, co["writer"], "Článek o serverech", "Mám dvě verze, rozhodněte, kterou vydat?")
    assert out["reviewer_id"] == ceo.actor_id
    # A board topic is strategic.
    out = _hand_in(conn, ceo, co["writer"], "Podklady k poradě", "Podklady sepsány.\nOvěřeno: 5/5 bodů.", topic="board")
    assert out["reviewer_id"] == ceo.actor_id
    # The work of a team member below a head goes to that head, not the CEO.
    out = _hand_in(conn, ceo, co["se"], "Upravit popisek tlačítka", "Popisek upraven v nastavení.\nOvěřeno: stránka ukazuje nový text.")
    assert out["reviewer_id"] in (co["cto"].actor_id, co["qa"].actor_id, None) or out["status"] == "done"


def test_without_a_general_reviewer_the_qa_reviewer_takes_the_ceos_routine_reviews(conn, owner, co):
    conn.execute("UPDATE actors SET archived_at = '2026-10-01' WHERE id = ?", (co["coo"].actor_id,))
    conn.commit()
    out = _hand_in(conn, co["ceo"], co["writer"], "Článek o zálohách", "Článek napsán, 900 slov.\nOvěřeno: zadání 3/3.")
    assert out["reviewer_id"] == co["qa"].actor_id


# ------------------------------------------------------------------ the packet

def test_a_review_item_is_served_with_a_packet_dod_evidence_and_a_checklist(conn, owner, co, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (repo / "parser.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "Fix the parser")
    sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    monkeypatch.setenv("POS_REVIEW_REPOS", str(repo))
    out = _hand_in(conn, co["cto"], co["se"], "Opravit parser", f"Commit {sha} na agent/dev.\nOvěřeno: pytest 9 passed",
                   definition_of_done="Parser nepadá na prázdném vstupu; test existuje.")
    assert out["reviewer_id"] == co["qa"].actor_id
    item = _item(conn, out["id"])
    served = api_worker._next_work(conn, co["qa"])["task"]
    assert served["id"] == item["id"]
    notes = served["notes"]
    assert "# Review packet: " + out["ref"] in notes
    assert "Parser nepadá na prázdném vstupu" in notes and "Ověřeno: pytest 9 passed" in notes
    assert "parser.py" in notes and "Fix the parser" in notes  # the diff stat from the repository
    assert "Checklist (code)" in notes and "review_task" in notes
    assert "run_size" not in served  # code runs at the agent's own effort
    assert served["review_packet"]["kind"] == "code"


def test_routine_reviews_are_batched_and_run_small(conn, owner, co):
    refs = []
    for i in range(6):  # incoming mails triaged with something to act on: routine, not auto-accepted
        out = _hand_in(conn, co["ceo"], co["writer"], f"Mail od dodavatele {i}",
                       f"Dodavatel {i}: problém s dodáním od listopadu, předávám COO k posouzení.",
                       source=f"event:gmail:{i}")
        assert out["status"] == "review"
        refs.append(out)
    reviewer = refs[0]["reviewer_id"]
    assert reviewer != co["ceo"].actor_id
    items = [_item(conn, r["id"]) for r in refs]
    assert all(it is not None and it["assignee_id"] == reviewer for it in items)
    who = Ctx(reviewer, via="worker")
    served = api_worker._next_work(conn, who)["task"]
    assert served["review_packet"]["kind"] == "routine"
    assert len(served["review_packet"]["batch"]) == review_packet.BATCH_MAX - 1
    assert served["notes"].startswith(f"# Review batch: {review_packet.BATCH_MAX} results")
    assert served["run_size"] == "S"  # low effort, the small cap
    # Reviewing a batched result closes its own item: it never starts a run of its own.
    batched = served["review_packet"]["batch"][0]
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (batched,)).fetchone()
    tasks.review(conn, who, review_work.reviewed_id(row), True, "OK")
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (batched,)).fetchone()[0] == "done"
    # A result asking for a decision is neither batched nor run small.
    out = _hand_in(conn, co["ceo"], co["writer"], "Mail od banky", "Banka nabízí nový tarif. Rozhodněte, zda přejít?",
                   source="event:gmail:x")
    assert not review_packet.batchable(conn, conn.execute("SELECT * FROM tasks WHERE id = ?", (out["id"],)).fetchone(),
                                       out["progress_note"])


def test_a_routine_packet_without_decisions_runs_at_size_s(conn, owner, co):
    out = _hand_in(conn, co["ceo"], co["writer"], "Report o návštěvnosti",
                   "Návštěvnost 1 200, +5 %. Zdroj: analytika.", topic="report")
    if out["status"] == "done":  # auto-accepted (a document): nothing to review
        return
    item = _item(conn, out["id"])
    pk = review_packet.build(conn, item)
    assert pk["kind"] == "docs" and pk["size"] == "S"


def test_commits_named_only_near_git_words():
    assert review_packet.commits_named("Commit abc1234 na agent/dev") == ["abc1234"]
    assert review_packet.commits_named("Faktura 2026100 za září") == []
    assert review_packet.commits_named("merge 1a2b3c4d..9f8e7d6c") == ["1a2b3c4d", "9f8e7d6c"]


# ------------------------------------------------------------------ the cache TTL

def _runs(conn, actor_id, gaps_min: list[float], cw_hit: int, cw_miss: int):
    t = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    for g in [0.0, *gaps_min]:
        t = t + timedelta(minutes=g)
        end = t + timedelta(minutes=1)
        rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at, ended_at, engine) VALUES "
                           "(?, 'task', 'ok', ?, ?, 'claude')", (actor_id, t.isoformat(), end.isoformat())).lastrowid
        cw = cw_hit if g and g < 5 else cw_miss
        conn.execute("INSERT INTO engine_usage (at, engine, actor_id, run_id, input_tokens, output_tokens, cost_usd, "
                     "cache_read_tokens, cache_creation_tokens) VALUES (?, 'claude', ?, ?, ?, 100, 0.1, 1000, ?)",
                     (end.isoformat(), actor_id, rid, cw, cw))
        t = end
    conn.commit()


def test_a_frequent_agent_with_a_big_prefix_gets_1h_a_rare_one_5m(conn, co):
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    # Every 20 minutes (a few under 5), small runs, a big stable prefix: 1 hour pays.
    _runs(conn, co["coo"].actor_id, [20, 3, 20, 25, 2, 30, 20, 3, 20, 15], cw_hit=3000, cw_miss=28000)
    assert cache_policy.ttl_for(conn, co["coo"].actor_id, now) == "1h"
    # Hours apart, big runs: every 1h write is paid twice and never read.
    _runs(conn, co["cfo"].actor_id, [180, 2, 240, 300, 3, 200, 400, 2, 600, 300], cw_hit=15000, cw_miss=20000)
    assert cache_policy.ttl_for(conn, co["cfo"].actor_id, now) == "5m"
    # Too few runs: the CLI's own default.
    assert cache_policy.ttl_for(conn, co["writer"].actor_id, now) is None
    r = cache_policy.ratio(conn, "2026-10-01", "2026-10-03")
    assert r["runs"] == 22 and r["read_per_write"] > 0


def test_the_worker_sets_the_cache_ttl_and_runs_a_sized_review_small(monkeypatch):
    from pos_worker import __main__ as wm

    monkeypatch.delenv("WORKER_CLAUDE_CACHE_TTL", raising=False)
    assert wm.cache_env({"cache_ttl": "1h"}) == {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}
    assert wm.cache_env({"cache_ttl": "1h", "profile": {"cache_ttl": "5m"}}) == {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}
    assert wm.cache_env({"cache_ttl": None}) == {}
    monkeypatch.setenv("WORKER_CLAUDE_CACHE_TTL", "5m")
    assert wm.cache_env({}) == {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}
    assert wm.cache_env({"cache_ttl": "bogus"}) == {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}

    from pos_worker import triage

    assert triage.size_settings("S", "medium", 10.0)["effort"] == "low"
