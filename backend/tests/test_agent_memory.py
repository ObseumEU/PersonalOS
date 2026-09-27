"""Every agent's pinned memory (pos.agent_memory): it writes its own, capped and
versioned, the worker reads it fresh for each run and puts it into the prompt."""

import pytest
from fastapi.testclient import TestClient

from pos import actors, agent_memory, agents, mcp_server
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="m" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, owner, name="Pamětník", purpose="x", lifetime="long_lived",
                               data_dir=tmp_path)
    other = agents.create_agent(conn, owner, name="Soused", purpose="x", lifetime="long_lived",
                                data_dir=tmp_path)["agent"]["id"]
    yield {"conn": conn, "owner": owner, "agent": made["agent"]["id"], "key": made["api_key"], "other": other,
           "client": client}
    conn.close()
    client.__exit__(None, None, None)


def test_an_agent_keeps_its_own_capped_versioned_memory(env):
    conn, a = env["conn"], env["agent"]
    assert agent_memory.get(conn, a)["body"] == ""
    agent_memory.set_body(conn, Ctx(a), "# HA\n- verze 2026.9.3")
    out = agent_memory.set_body(conn, Ctx(a), "# HA\n- verze 2026.9.4\n- HA OS")
    assert out["body"].endswith("HA OS") and out["max_chars"] == agent_memory.MAX_CHARS
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE actor_id = ? AND archived_at IS NULL",
                        (a,)).fetchone()[0] == 1
    row = conn.execute("SELECT id FROM memories WHERE actor_id = ?", (a,)).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM history WHERE entity = 'memory' AND entity_id = ?",
                        (row["id"],)).fetchone()[0] == 2
    with pytest.raises(agent_memory.MemoryError, match="limit"):
        agent_memory.set_body(conn, Ctx(a), "x" * (agent_memory.MAX_CHARS + 1))
    with pytest.raises(Forbidden):  # another agent cannot write it
        agent_memory.set_body(conn, Ctx(env["other"]), "přepsáno", agent_id=a)
    agent_memory.set_body(conn, env["owner"], "od majitele", agent_id=a)  # the owner can
    assert agent_memory.text(conn, a) == "od majitele"
    assert agent_memory.text(conn, env["other"]) == ""
    for tool in ("memory_get", "memory_update"):
        assert tool in mcp_server.tool_names() and mcp_server.may_use(conn, a, tool)


def test_the_worker_gets_it_and_the_prompt_shows_it(env):
    agent_memory.set_body(env["conn"], Ctx(env["agent"]), "- SSH uživatel: hassio")
    r = env["client"].get("/api/worker/memory", headers={"Authorization": f"Bearer {env['key']}"})
    assert r.status_code == 200 and r.json()["body"] == "- SSH uživatel: hassio"

    pytest.importorskip("pos_worker")
    from pos_worker.prompt import build_task_prompt, stable_prompt

    me = {"name": "Pamětník", "instructions": "Dělej věci.", "memory": r.json()["body"]}
    text = build_task_prompt(me, {"ref": "T-001", "title": "Prozkoumej"}, [], include_guardrails=False)
    assert "# Your memory" in text and "SSH uživatel: hassio" in text
    assert text.index("# Your memory") < text.index("# Your task T-001")
    assert "hassio" not in stable_prompt(me)  # the cached system prompt stays the same bytes
    empty = build_task_prompt({**me, "memory": ""}, {"ref": "T-2", "title": "x"}, [], include_guardrails=False)
    assert "memory_update" in empty
    old = build_task_prompt({k: v for k, v in me.items() if k != "memory"}, {"ref": "T-3", "title": "x"}, [],
                            include_guardrails=False)
    assert "# Your memory" not in old
