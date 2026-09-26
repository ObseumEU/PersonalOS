"""Agents as code (REVIZE-FUNKCI 3.7)."""

import json

from pos import actors, agents, agents_code, org
from pos.db import connect, migrate


def _repo(tmp_path):
    base = tmp_path / "agents"
    for slug, spec in {
        "writer": {"name": "Writer", "role": "specialist", "team": "content", "reports_to": "Project manager",
                   "permissions": ["tasks:read", "tasks:claim", "agents:create"], "worker": "writer"},
        "hr-agent": {"name": "HR agent", "runtime": "builtin", "worker": "none"},
        "discord": {"name": "Discord bot", "enabled": False, "worker": "discord"},
    }.items():
        (base / slug).mkdir(parents=True)
        (base / slug / "agent.json").write_text(json.dumps(spec), encoding="utf-8")
    return base


def test_role_agents_come_from_their_files_and_workers_get_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")
    c = connect(tmp_path / "a.db")
    migrate(c)
    actors.ensure_builtin(c)
    org.ensure(c)
    base = _repo(tmp_path)
    out = agents_code.ensure_from_repo(c, tmp_path, base)
    assert out["created"] == ["Writer"]
    w = actors.find_by_name(c, "Writer")
    assert w["role"] == "specialist" and w["team"] == "content" and w["reports_to"] == org.pm_id(c)
    assert "agents:create" not in agents.permissions_of(c, w["id"])  # never from a file
    assert actors.find_by_name(c, "Discord bot") is None  # disabled
    assert agents_code.ensure_from_repo(c, tmp_path, base)["created"] == []  # idempotent
    keys = tmp_path / "keys"
    # the Project manager has no file here: it gets its worker in the agent pool (pos.workers)
    assert agents_code.write_worker_keys(c, keys, base) == ["writer", "pool/project-manager"]
    key = (keys / "writer" / "key").read_text().strip()
    assert actors.actor_for_key(c, key) == w["id"]
    assert agents_code.write_worker_keys(c, keys, base) == []  # a valid key stays
    pm_key = (keys / "pool" / "project-manager" / "key").read_text().strip()
    assert actors.actor_for_key(c, pm_key) == org.pm_id(c)
    c.execute("UPDATE actors SET paused_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (org.pm_id(c),))
    agents_code.write_worker_keys(c, keys, base)
    assert not (keys / "pool" / "project-manager").exists()  # paused: the pool stops its worker
    c.close()


def test_worker_reads_its_key_file(tmp_path, monkeypatch):
    from pos_worker.__main__ import agent_key

    monkeypatch.delenv("POS_AGENT_KEY", raising=False)
    (tmp_path / "key").write_text("pos_abc\n")
    monkeypatch.setenv("POS_AGENT_KEY_FILE", str(tmp_path / "key"))
    assert agent_key(wait_s=0) == "pos_abc"
    import os

    assert os.environ["POS_AGENT_KEY"] == "pos_abc"


def test_the_repository_files_are_valid():
    specs = {s["slug"]: s for s in agents_code.specs()}
    assert {"dev-agent", "mail-agent", "agent-coach", "project-manager"} <= set(specs)
    for s in specs.values():
        assert set(s.get("permissions", [])) <= set(agents.PERMISSIONS), s["slug"]
