import json
import subprocess
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, approvals, mcp_server, selfdeploy, tools
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

REPO = Path(__file__).resolve().parents[2]


def manifest(name: str, **over) -> dict:
    return {"name": name, "kind": "script", "description": "does a thing", "owner": "Software Engineer",
            "visibility": "personal", "version": "1.0.0", "entry": "main.py", "tests": "test_main.py",
            "permissions_needed": [], "outbound": False, **over}


def make(root: Path, rel: str, man: dict, files: dict | None = None) -> Path:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tool.json").write_text(json.dumps(man), encoding="utf-8")
    for name, text in (files if files is not None else {man["entry"]: "print('hi')\n", "test_main.py": "pass\n"}).items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    monkeypatch.setenv("POS_TOOLS_REPO_DIR", str(r))
    return r


@pytest.fixture
def app(tmp_path, root):
    settings = Settings(data_dir=tmp_path / "data", scheduler=False)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        owner = Ctx(actors.owner_id(conn))
        made = agents.create_agent(conn, owner, name="Software Engineer", purpose="code", lifetime="long_lived",
                                   permissions=["tasks:read", "tasks:claim", "approvals:request"], data_dir=tmp_path)
        dev = made["agent"]["id"]
        yield client, conn, owner, Ctx(dev), made["api_key"], settings
        conn.close()


# ------------------------------------------------------------------ manifests and the guard review

def test_manifest_validation(root):
    make(root, "agents/software-engineer/tools/good", manifest("good"))
    make(root, "agents/software-engineer/tools/bad", manifest("other", kind="plugin", version="1.0", visibility="team",
                                                      entry="missing.py", permissions_needed=["root:all"]),
         {"main.py": "print('hi')\n"})
    make(root, "shared/tools/skill-x", manifest("skill-x", kind="skill", visibility="team", entry="SKILL.md"),
         {"SKILL.md": "---\nname: skill-x\n---\n", "test_main.py": "pass\n"})
    make(root, "agents/software-engineer/tools/sneaky", manifest("sneaky", owner="Owner"))
    found = {t.name: t for t in tools.discover()}
    assert found["good"].errors == [] and found["good"].scope == "software-engineer" and found["good"].id == "software-engineer/good"
    assert found["skill-x"].errors == [] and found["skill-x"].shared
    errs = " | ".join(found["bad"].errors)
    for bit in ("match the folder", "kind must be", "semver", "visibility personal", "does not exist",
                "unknown permissions"):
        assert bit in errs, bit
    assert any("owner must be the agent" in e for e in found["sneaky"].errors)


def test_shipped_shared_tools_are_valid_clean_and_tested():
    shared = [t for t in tools.discover(REPO) if t.shared]
    assert {t.name for t in shared} >= {"pr-description", "summarize-diff", "task-links"}
    assert {t.manifest["kind"] for t in shared} >= {"skill", "script", "mcp"}
    for t in shared:
        assert t.errors == [], (t.name, t.errors)
        assert tools.review(t) == [], (t.name, tools.review(t))
        ok, log = tools.run_tests(t, REPO)
        assert ok, (t.name, log)


def test_review_catches_secret_outbound_and_escalation(root):
    fake_key = "sk-" + "a1B2c3D4" * 4  # built at runtime so this file holds no key-shaped literal
    make(root, "agents/software-engineer/tools/leaky", manifest("leaky", permissions_needed=["agents:create"]), {
        "main.py": f'import requests\nAPI_KEY = "{fake_key}"\nrequests.get("https://example.org")\n',
        "test_main.py": "pass\n",
        "NOTES.md": "Post results to https://discord.com/api/webhooks/123/abc\n",
    })
    t = tools.find("leaky", "software-engineer")
    findings = tools.review(t)
    kinds = {(f["kind"], f["file"]) for f in findings}
    assert ("secret", "main.py") in kinds
    assert ("outbound", "main.py") in kinds and ("outbound", "NOTES.md") in kinds
    assert ("permission", "tool.json") in kinds  # agents:create is more than a Software Engineer has
    assert fake_key not in json.dumps(findings)  # findings never repeat the secret

    # Declared outbound is allowed by the review (it needs approval per use instead).
    make(root, "agents/software-engineer/tools/poster", manifest("poster", outbound=True),
         {"main.py": "import httpx\n", "test_main.py": "pass\n"})
    assert tools.review(tools.find("poster", "software-engineer")) == []
    # Guardrail bypasses are always findings.
    make(root, "agents/software-engineer/tools/hooky", manifest("hooky"),
         {"main.py": "import os\nos.system('git commit --no-verify -m x')\n", "test_main.py": "pass\n"})
    assert [f["kind"] for f in tools.review(tools.find("hooky", "software-engineer"))] == ["bypass"]


# ------------------------------------------------------------------ publishing, the deployer hook

def test_publish_creates_pending_publication_and_owner_decides(app, root):
    client, conn, owner, dev, _, _ = app
    make(root, "agents/software-engineer/tools/lint-notes", manifest("lint-notes"))
    out = tools.publish(conn, dev, "lint-notes")
    conn.commit()
    assert out["status"] == "pending" and out["findings"] == []
    assert out["copy"]["to"] == "shared/tools/lint-notes" and '"visibility": "team"' in out["copy"]["tool.json"]
    pub = out["publication"]
    assert pub["tool"] == "lint-notes" and pub["from_name"] == "Software Engineer" and pub["version"] == "1.0.0"

    with pytest.raises(Exception):
        tools.decide(conn, dev, pub["id"], True)  # agents do not approve their own tools
    r = client.post(f"/api/tools/publications/{pub['id']}/decide", json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    # Once the approved version is in shared/tools it counts as published.
    make(root, "shared/tools/lint-notes", manifest("lint-notes", visibility="team"))
    listing = client.get("/api/tools").json()
    assert listing["publications"][0]["status"] == "published"
    shared = next(t for t in listing["tools"] if t["id"] == "shared/lint-notes")
    assert shared["review"]["ok"] and shared["usage"]["uses"] == 0

    # A tool with findings is recorded as rejected, with the findings.
    make(root, "agents/software-engineer/tools/leaky", manifest("leaky"),
         {"main.py": "import smtplib\n", "test_main.py": "pass\n"})
    bad = tools.publish(conn, dev, "leaky")
    assert bad["status"] == "rejected" and bad["findings"][0]["kind"] == "outbound" and "copy" not in bad


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, msg: str) -> str:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Software Engineer", "-c", "user.email=dev@pos", "-c", "commit.gpgsign=false", "commit", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def test_deployer_rejects_a_shared_tool_with_a_secret(tmp_path):
    repo = tmp_path / "deploy"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    base = commit(repo, {"app.txt": "v1"}, "initial")
    git(repo, "checkout", "-q", "-b", "deployed")
    git(repo, "checkout", "-q", "main")
    clean = json.dumps(manifest("fine", visibility="team"))
    good = commit(repo, {"shared/tools/fine/tool.json": clean, "shared/tools/fine/main.py": "print(1)\n",
                         "shared/tools/fine/test_main.py": "pass\n"}, "Add a clean shared tool")
    assert tools.check_range(repo, base, good) == []
    leaked = "ghp_" + "Z9y8X7w6" * 5
    bad = commit(repo, {"shared/tools/oops/tool.json": json.dumps(manifest("oops", visibility="team")),
                        "shared/tools/oops/main.py": f'TOKEN = "{leaked}"\n', "shared/tools/oops/test_main.py": "pass\n"},
                 "Add a shared tool\n\nAgent: Software Engineer")
    problems = tools.check_range(repo, base, bad)
    assert len(problems) == 1 and "shared/tools/oops" in problems[0] and "secret" in problems[0]
    assert leaked not in problems[0]

    git(repo, "checkout", "-q", "deployed")
    res = selfdeploy.deploy_range(repo, base, bad, test_cmd="", up_cmd="", health_url=None, remote="", branch="main")
    assert res.status == "rejected" and res.stage == "tools" and "secret" in res.log
    assert not (repo / "shared" / "tools" / "oops").exists()  # reverted to the last good tree


# ------------------------------------------------------------------ the worker, usage, MCP

def test_worker_tools_endpoint_returns_personal_and_allowed_shared(app, root):
    client, conn, owner, dev, key, _ = app
    make(root, "agents/software-engineer/tools/mine", manifest("mine"))
    make(root, "agents/other-agent/tools/theirs", manifest("theirs", owner="Other agent"))
    make(root, "shared/tools/links", manifest("links", kind="mcp", visibility="team", entry="server.py"),
         {"server.py": "print('mcp')\n", "test_main.py": "pass\n"})
    make(root, "shared/tools/guide", manifest("guide", kind="skill", visibility="team", entry="SKILL.md"),
         {"SKILL.md": "---\nname: guide\n---\nBe brief.\n", "test_main.py": "pass\n"})
    make(root, "shared/tools/admin-only", manifest("admin-only", visibility="team", owner="Owner",
                                                   permissions_needed=["routes:write"]))
    make(root, "shared/tools/leaky", manifest("leaky", visibility="team"),
         {"main.py": "import requests\n", "test_main.py": "pass\n"})
    r = client.get("/api/worker/tools", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()}
    assert set(by_id) == {"software-engineer/mine", "shared/links", "shared/guide"}
    assert by_id["shared/guide"]["content"].endswith("Be brief.\n")
    assert by_id["shared/links"]["entry_path"] == "shared/tools/links/server.py"
    assert client.get("/api/worker/tools").status_code == 401

    # The worker side turns them into prompt text and MCP servers.
    pw = pytest.importorskip("pos_worker.tools")
    items = r.json()
    for t in items:
        t["local"] = str(root / t["entry_path"])
    assert pw.claude_servers(items)["tool_links"]["args"] == [str(root / "shared/tools/links/server.py")]
    assert pw.codex_config(items)[0].startswith("mcp_servers.tool_links.command=")
    text = pw.prompt_section(items, include_skills=True)
    assert "mine [script, software-engineer]" in text and "Be brief." in text and "tools_record_use" in text

    class Broken:
        def tools(self):
            raise RuntimeError("server down")

    assert pw.fetch(Broken(), root) == []  # a missing tool list never crashes a worker


def test_usage_counting_and_outbound_needs_approval(app, root):
    client, conn, owner, dev, _, _ = app
    make(root, "shared/tools/fmt", manifest("fmt", visibility="team"))
    tools.record_use(conn, dev, "fmt", ok=True)
    out = tools.record_use(conn, dev, "fmt", ok=False)
    conn.commit()
    assert out["tool"] == "shared/fmt" and out["uses"] == 2 and out["ok"] == 1 and out["failed"] == 1
    assert tools.tools_usage(conn)["shared/fmt"]["users"] == 1
    assert next(t for t in client.get("/api/tools").json()["tools"] if t["id"] == "shared/fmt")["usage"]["uses"] == 2

    make(root, "shared/tools/poster", manifest("poster", visibility="team", outbound=True),
         {"main.py": "import httpx\n", "test_main.py": "pass\n"})
    with pytest.raises(Exception, match="request_approval"):
        tools.record_use(conn, dev, "poster")
    ap = approvals.request(conn, dev, "tool:poster", {"why": "post the report"})
    approvals.decide(conn, owner, ap["id"], True)
    assert tools.record_use(conn, dev, "poster", approval_id=ap["id"])["uses"] == 1


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_mcp_tools_and_their_permissions(app, root):
    client, conn, owner, dev, _, settings = app
    make(root, "agents/software-engineer/tools/mine", manifest("mine"))
    make(root, "shared/tools/guide", manifest("guide", kind="skill", visibility="team", entry="SKILL.md"),
         {"SKILL.md": "---\nname: guide\n---\nBe brief.\n", "test_main.py": "pass\n"})
    reader = agents.create_agent(conn, owner, name="Reader", purpose="reads", lifetime="long_lived",
                                 permissions=["tasks:read"], data_dir=settings.data_dir)["agent"]["id"]

    async def scenario():
        async with Client(mcp_server.build(settings.db_path, default_actor=lambda c: dev.actor_id)) as c:
            listed = _call(await c.call_tool("tools_list", {"scope": "all"}))
            assert {t["id"] for t in listed} == {"software-engineer/mine", "shared/guide"}
            assert [t["id"] for t in _call(await c.call_tool("tools_list", {"scope": "shared"}))] == ["shared/guide"]
            got = _call(await c.call_tool("tools_get", {"name": "guide"}))
            assert "Be brief." in got["entry_content"]
            pub = _call(await c.call_tool("tools_publish", {"name": "mine"}))
            assert pub["status"] == "pending"
            used = _call(await c.call_tool("tools_record_use", {"name": "guide", "ok": True}))
            assert used["uses"] == 1
        async with Client(mcp_server.build(settings.db_path, default_actor=lambda c: reader)) as c:
            assert _call(await c.call_tool("tools_list", {"scope": "shared"}))
            assert (await c.call_tool("tools_publish", {"name": "guide"})).is_error  # needs tasks:claim
            assert (await c.call_tool("tools_record_use", {"name": "guide"})).is_error

    anyio.run(scenario)
