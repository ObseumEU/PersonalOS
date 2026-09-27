"""The task detail's TL;DR (pos.task_summary) and the detail's related items."""

from fastapi.testclient import TestClient

from pos import actors, approvals, asks, comments, task_summary, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate


def _conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return c


def _fake_llm(monkeypatch, calls):
    def fake(conn, task):
        calls.append(task["id"])
        return f"Shrnutí {len(calls)}: {task['title']}.", None

    monkeypatch.setattr(task_summary, "_llm", fake)


def test_summary_is_cached_until_the_task_changes(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, me, {"title": "Pay the invoice", "notes": "Proč: the supplier asked.\n\nMore text."})
    calls: list[int] = []
    _fake_llm(monkeypatch, calls)

    first = task_summary.summary(conn, me, t["id"])
    assert first["source"] == "llm" and first["text"] == "Shrnutí 1: Pay the invoice."
    assert task_summary.summary(conn, me, t["id"])["text"] == first["text"]
    assert calls == [t["id"]]  # the second read came from the cache

    comments.add(conn, me, t["id"], "a remark")  # only the discussion grew: kept a while
    assert task_summary.summary(conn, me, t["id"])["text"] == first["text"]
    assert len(calls) == 1

    tasks.update(conn, me, t["id"], {"status": "waiting"})  # a real change: written again
    assert task_summary.summary(conn, me, t["id"])["text"].startswith("Shrnutí 2")
    assert len(calls) == 2


def test_fallback_without_a_model_is_the_first_paragraph(tmp_path):
    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, me, {"title": "X", "notes": "### Proč\n**Purpose:** find why *HA* went down.\n\nDetails."})
    out = task_summary.summary(conn, me, t["id"])  # POS_CLAUDE_DISABLED: no model
    assert out["source"] == "fallback"
    assert out["text"] == "find why *HA* went down." or out["text"] == "find why HA went down."
    # without generate, the cached one (or the fallback) comes back and nothing is written
    assert task_summary.summary(conn, me, t["id"], generate=False)["text"] == out["text"]


def test_summary_run_is_not_the_tasks_cost(tmp_path, monkeypatch):
    """The model call goes through the runner without a task_id: the platform pays, not the task."""
    from pos import runner

    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, me, {"title": "Summarise me", "notes": "Something long enough to summarise."})
    seen = {}

    def fake_run(c, req):
        seen["task_id"], seen["model"], seen["kind"] = req.task_id, req.model, req.kind
        return runner.RunResult(run_id=1, status="ok", output="  O co jde.  Kde to stojí. ")

    monkeypatch.setattr(runner, "available", lambda engine="codex": True)
    monkeypatch.setattr(runner, "run", fake_run)
    out = task_summary.summary(conn, me, t["id"])
    assert out == {**out, "text": "O co jde. Kde to stojí.", "source": "llm"}
    assert seen == {"task_id": None, "model": "claude-haiku-4-5", "kind": "task_summary"}


def test_endpoints_summary_board_and_related(tmp_path, monkeypatch):
    from pos.main import create_app

    settings = Settings(data_dir=tmp_path)
    calls: list[int] = []
    _fake_llm(monkeypatch, calls)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        me = Ctx(actors.owner_id(conn))
        ai = Ctx(actors.assistant_id(conn), via="mcp")
        src = tasks.create(conn, me, {"title": "Invoice template", "assignee": "ai", "status": "next",
                                      "notes": "See T-999 and the offer."})
        tasks.claim(conn, ai, src["id"])
        ticket = asks.ask(conn, ai, title="Pick a template", why="The client wants it today.",
                          options=["Classic", "Minimal"], recommendation="Minimal", task_id=src["id"])
        ap = approvals.request(conn, ai, "email_send", {"why": "Send the invoice"}, task_id=src["id"])
        conn.commit()
        conn.close()

        r = client.get(f"/api/tasks/{src['ref']}/summary")
        assert r.status_code == 200 and r.json()["source"] == "llm"
        board = client.get("/api/tasks?view=board").json()
        row = next(t for t in board if t["id"] == src["id"])
        assert row["summary"] == r.json()["text"]
        assert any(t["ref"] == ticket["ref"] and t["summary"] is None for t in board)

        rel = client.get(f"/api/tasks/{src['ref']}/related").json()
        assert [a["ref"] for a in rel["asks_open"]] == [ticket["ref"]]
        assert "### Možnosti" in rel["asks_open"][0]["notes"] and rel["asks_open"][0]["blocking"]
        assert [a["id"] for a in rel["approvals"]] == [ap["id"]]
        assert rel["ask_for"] is None and rel["mentioned"] == []  # T-999 does not exist

        back = client.get(f"/api/tasks/{ticket['ref']}/related").json()
        assert back["ask_for"]["task"]["ref"] == src["ref"] and back["ask_for"]["status"] == "open"
        assert client.get("/api/tasks/T-99999/summary").status_code == 404
