from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, engines, integrations, network
from pos.core import now_iso
from pos.db import connect, migrate
from pos.engine_view import pretty_model


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_CODEX_MODEL", "gpt-5-codex")
    monkeypatch.delenv("POS_AGENT_RUNTIME", raising=False)
    c = connect(tmp_path / "e.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    yield c
    c.close()


def test_pretty_model():
    assert pretty_model("claude-opus-5-5") == "Opus 5.5"
    assert pretty_model("claude-sonnet-5") == "Sonnet 5"
    assert pretty_model("claude-opus-5-5[1m]") == "Opus 5.5 1M"
    assert pretty_model("gpt-5-codex") == "gpt-5-codex"


def _view(conn, agent_id):
    return next(a for a in agents.overview(conn) if a["id"] == agent_id)["engine_view"]


def test_auto_agent_shows_codex_then_claude_fallback(conn):
    aid = actors.assistant_id(conn)
    v = _view(conn, aid)
    assert v["setting"] == "auto" and v["last_run"] is None
    assert v["now"]["label"] == "Codex · gpt-5-codex"

    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    engines.pause(conn, "codex", until, "Codex usage limit")
    conn.execute("INSERT INTO runs (actor_id, kind, status, started_at, engine) VALUES (?, 'task', 'ok', ?, 'claude')",
                 (aid, now_iso()))
    v = _view(conn, aid)
    assert v["now"]["label"] == "Claude · Opus 5.5 · fallback" and v["now"]["fallback"]
    assert v["last_run"]["label"] == "Claude · Opus 5.5 · fallback"
    node = next(n for n in network.build(conn)["nodes"] if n["id"] == aid)
    assert node["engine_view"]["now"]["engine"] == "claude"


def test_pinned_claude_is_not_a_fallback(conn):
    aid = actors.assistant_id(conn)
    conn.execute("UPDATE actors SET engine = 'claude', model = 'claude-sonnet-5' WHERE id = ?", (aid,))
    v = _view(conn, aid)
    assert v["now"]["label"] == "Claude · Sonnet 5" and not v["now"]["fallback"]
    assert _view(conn, actors.owner_id(conn)) is None
