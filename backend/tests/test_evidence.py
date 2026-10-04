"""Evidence for a hand-in (pos.evidence): a done-claim carries a sha, a sent message, a URL, a file
or test output that checks out; goal numbers match the metrics; people and review items are not gated."""

import json
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, audit, evidence, goals, review_policy, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

WORKER = ["tasks:read", "tasks:claim", "messages:send", "approvals:request"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_EVIDENCE_CHECKS", "1")
    monkeypatch.setenv("POS_EVIDENCE_REPOS", str(tmp_path / "no-repo-here"))
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="e" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, owner, name="Writer", purpose="Writer", lifetime="long_lived",
                               permissions=list(WORKER), data_dir=tmp_path)
    ceo = agents.create_agent(conn, owner, name="CEO", purpose="CEO", lifetime="long_lived",
                              permissions=[*WORKER, "tasks:write", "tasks:review"], data_dir=tmp_path)
    conn.execute("UPDATE actors SET role = 'ceo' WHERE id = ?", (ceo["agent"]["id"],))
    conn.commit()
    yield {"conn": conn, "owner": owner, "agent": made["agent"]["id"], "ceo": Ctx(ceo["agent"]["id"]),
           "tmp": tmp_path}
    conn.close()
    client.__exit__(None, None, None)


def _task(env, **fields):
    conn, a = env["conn"], env["agent"]
    t = tasks.create(conn, env["ceo"], {"title": "Úprava webu", "topic": "obchod",
                                          "assignee": {"type": "agent", "id": a}, **fields})
    tasks.claim(conn, Ctx(a, via="mcp"), t["id"])
    return t


def _hand_in(env, note, **fields):
    t = _task(env, **fields)
    return tasks.complete(env["conn"], Ctx(env["agent"], via="mcp"), t["id"], note)


def _actions(conn, task_id):
    return [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE entity = 'task' AND entity_id = ? "
                                       "AND action LIKE 'handin_evidence%' ORDER BY id", (task_id,))]


def _git_repo(path):
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    return run("rev-parse", "HEAD").stdout.strip()


# ------------------------------------------------------------------ claims and extraction

def test_claims_and_items_are_found():
    assert evidence.claim("Hotovo, nasazeno na prod.") == "nasazeno"
    assert evidence.claim("Fixed the login.") == "Fixed"
    assert evidence.claim("E-mail zatím nebyl odeslán, čeká na schválení.") is None
    assert evidence.claim("Nothing sent yet.") is None
    assert evidence.claim("Splňuje definition of done? Ověřím zítra.") is None
    found = evidence.extract("Commit 3f2a9c1d, viz https://example.com/a?b=1. Zdroj: doc1a2b3c4d:c5, "
                             "soubor #12, poznámka 7, outbound #44, message id: <abc@mail.x>, run 2026-10-04")
    assert found["sha"] == ["3f2a9c1d"] and found["url"] == ["https://example.com/a?b=1"]
    assert found["file"] == ["12"] and found["note"] == ["7"] and found["outbound"] == ["44"]
    assert found["message_id"] == ["abc@mail.x"]


# ------------------------------------------------------------------ a commit sha

def test_a_sha_is_verified_in_a_git_repo(env, monkeypatch):
    sha = _git_repo(env["tmp"] / "repo")
    monkeypatch.setenv("POS_EVIDENCE_REPOS", str(env["tmp"] / "repo"))
    assert evidence.check_sha(sha[:10]).status == "verified"
    assert evidence.check_sha("deadbee1234").status == "failed"
    assert evidence.check_sha(sha, []).status == "unverifiable"  # no repository reachable: fail open
    assert evidence.check_sha(sha, [env["tmp"] / "nothing"]).status == "unverifiable"

    out = _hand_in(env, f"Opraveno, commit {sha[:12]}. Ověřeno: stránka se načte.")
    assert "Doložte" not in (out.get("platform_note") or "")
    assert _actions(env["conn"], out["id"]) == ["handin_evidence_ok"]

    out = _hand_in(env, "Opraveno, commit deadbee1234. Ověřeno: stránka se načte.")
    assert "Doložte" in out["platform_note"] and "deadbee1234" in out["platform_note"]
    assert _actions(env["conn"], out["id"]) == ["handin_evidence_failed"]


def test_a_deployed_sha_needs_no_repo(env):
    conn = env["conn"]
    conn.execute("INSERT INTO deploys (old_sha, new_sha, status, created_at) VALUES ('a1b2c3d4e5', 'f6e5d4c3b2a1', "
                 "'ok', '2026-10-04T00:00:00+00:00')")
    out = _hand_in(env, "Nasazeno: f6e5d4c3b2a1.")
    assert "platform_note" not in out or "Doložte" not in out["platform_note"]


# ------------------------------------------------------------------ a sent message

def test_an_outbound_id_is_verified_in_the_log(env):
    conn, a = env["conn"], env["agent"]
    audit.log(conn, Ctx(a), "outbound:email.send", None, None, status="sent", summary="to x@y.cz: Nabídka")
    sent = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    audit.log(conn, Ctx(a), "outbound:email.send", None, None, status="failed", error="smtp down")
    failed = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    assert evidence._check_outbound_id(conn, str(sent)).status == "verified"
    assert evidence._check_outbound_id(conn, str(failed)).status == "failed"
    assert evidence._check_outbound_id(conn, "999999").status == "failed"

    out = _hand_in(env, f"Odesláno, outbound #{sent}.")
    assert "Doložte" not in (out.get("platform_note") or "")
    out = _hand_in(env, f"Odesláno, outbound #{failed}.")
    assert "Doložte" in out["platform_note"]
    # A message id the provider returned (read defensively from the audit detail).
    audit.log(conn, Ctx(a), "outbound:email.send", None, None, status="sent", message_id="<m1@mail.obseum.cz>")
    assert evidence._check_message_id(conn, "m1@mail.obseum.cz").status == "verified"


def test_a_send_the_task_did_counts_by_itself(env):
    conn, a = env["conn"], env["agent"]
    t = _task(env)
    audit.log(conn, Ctx(a), "outbound:discord.post", "task", t["id"], status="sent")
    out = tasks.complete(conn, Ctx(a, via="mcp"), t["id"], "Odesláno do Discordu.")
    assert "Doložte" not in (out.get("platform_note") or "")


# ------------------------------------------------------------------ a URL

@pytest.fixture
def web(monkeypatch):
    """HEAD/GET answered by a mock: path -> status; public DNS assumed."""
    answers = {}

    def handler(request):
        code = answers.get(request.url.path, 200)
        if isinstance(code, dict):
            code = code.get(request.method, 200)
        return httpx.Response(code)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(evidence, "_public_host", lambda host: True)
    return answers


def test_a_url_is_checked_with_head(env, web):
    web.update({"/gone": 404, "/broken": 500, "/nohead": {"HEAD": 405, "GET": 200}, "/x": 404})
    assert evidence.check_url("https://example.cz/ok").status == "verified"
    assert evidence.check_url("https://example.cz/gone").status == "failed"
    assert evidence.check_url("https://example.cz/broken").status == "failed"
    assert evidence.check_url("https://example.cz/nohead").status == "verified"
    assert evidence.check_url("https://github.com/x").status == "unverifiable"  # a private repo looks missing
    assert evidence.check_url("ftp://example.cz/a").status == "unverifiable"

    out = _hand_in(env, "Nasazeno, viz https://example.cz/ok.")
    assert "Doložte" not in (out.get("platform_note") or "")
    out = _hand_in(env, "Nasazeno, viz https://example.cz/gone.")
    assert "Doložte" in out["platform_note"] and "HTTP 404" in out["platform_note"]


def test_private_hosts_are_never_requested(monkeypatch):
    def boom(**kw):
        raise AssertionError("no request to a private host")

    monkeypatch.setattr(httpx, "Client", boom)
    for url in ("http://127.0.0.1:8000/api/health", "http://192.168.1.10/", "http://169.254.169.254/latest"):
        assert evidence.check_url(url).status == "unverifiable"


def test_slow_checks_stay_within_the_budget(env, monkeypatch):
    monkeypatch.setattr(evidence, "check_url", lambda url: (time.sleep(2), None)[1])
    started = time.monotonic()
    res = evidence.check(env["conn"], "Nasazeno: https://slow.example.cz/", budget_s=0.3)
    assert time.monotonic() - started < 1.5
    assert res.status == "verified" and res.items[0].status == "unverifiable"  # fail open


# ------------------------------------------------------------------ no evidence

def test_a_done_claim_without_evidence_is_sent_back_and_never_auto_accepted(env):
    conn = env["conn"]
    note = "Hotovo, web upraven.\nOvěřeno: zkontrolováno."
    out = _hand_in(env, note, estimate_min=30)  # small and verified: the policy would accept it
    assert out["status"] == "review"
    assert out["platform_note"].startswith("Doložte výsledek") and "sha commitu" in out["platform_note"]
    assert _actions(conn, out["id"]) == ["handin_evidence_missing"]
    assert conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND kind = 'system' AND body LIKE '%Doložte%'",
                        (out["id"],)).fetchone()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (out["id"],)).fetchone()
    assert evidence.flagged(conn, out["id"]) and review_policy.decide(conn, row, note).action != "accept"
    assert evidence.nudges(conn, env["agent"])
    # The same small result with its test output is accepted at once.
    out = _hand_in(env, "Hotovo, testy prošly: 12 passed.\nOvěřeno: 12 passed.", estimate_min=30)
    assert out["status"] == "done" and "platform_note" not in out


def test_documents_and_routine_checks_need_no_evidence_for_a_plain_done(env):
    out = _hand_in(env, "Hotovo, souhrn odeslán v DM.", title="Souhrn pro majitele", topic="digest")
    assert "Doložte" not in (out.get("platform_note") or "")
    assert _actions(env["conn"], out["id"]) == []


def test_people_and_review_items_are_not_gated(env):
    conn, owner = env["conn"], env["owner"]
    t = tasks.create(conn, owner, {"title": "Moje věc", "assignee": "me"})
    out = tasks.complete(conn, owner, t["id"], "Hotovo, odesláno.")
    assert out["status"] == "done" and _actions(conn, t["id"]) == []
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone()
    assert evidence.on_hand_in(conn, owner, row, "Hotovo.") is None
    out = _hand_in(env, "Hotovo, výsledek přijat.", source="review:1")
    assert "Doložte" not in (out.get("platform_note") or "") and _actions(conn, out["id"]) == []


# ------------------------------------------------------------------ goal numbers

def test_a_goal_number_that_contradicts_the_metrics_is_flagged(env):
    conn = env["conn"]
    goals.create(conn, env["owner"], {"title": "Růst tržeb", "metric": "MRR", "baseline": 5000,
                                      "current": 12000, "target_value": 50000})
    assert evidence.contradictions(conn, "MRR je 12 000 Kč, cíl 50 000.") == []
    assert evidence.contradictions(conn, "MRR: 50000 do konce roku") == []  # the target may be quoted
    assert evidence.contradictions(conn, "MRR 2026: rosteme") == []          # a year, not the number
    found = evidence.contradictions(conn, "Stav: MRR je 15 000 Kč.")
    assert len(found) == 1 and "12000" in found[0] and "15 000" in found[0] and "Růst tržeb" in found[0]
    assert evidence.contradictions(conn, "Růst tržeb: 15 %") and not evidence.contradictions(conn, "Růst tržeb: 16 %")

    # The owner report's text (complete_task `report`) is read too.
    t = _task(env)
    with evidence.extra_text(evidence.report_text({"takeaway": "Hotovo.", "content": "MRR je 15 000 Kč",
                                                   "verification": "12 passed"})):
        out = tasks.complete(conn, Ctx(env["agent"], via="mcp"), t["id"], "Hotovo, 12 passed.")
    assert "Opravte čísla" in out["platform_note"] and "15 000" in out["platform_note"]
    assert _actions(conn, t["id"]) == ["handin_evidence_failed"]
    detail = json.loads(conn.execute("SELECT detail FROM audit_log WHERE action = 'handin_evidence_failed' "
                                     "AND entity_id = ?", (t["id"],)).fetchone()[0])
    assert detail["contradictions"]
