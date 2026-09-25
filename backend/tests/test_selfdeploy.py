import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, selfdeploy, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

CHECK = "import sys; sys.exit(0 if open('app.txt').read().startswith('good') else 1)"


def git(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def commit(repo: Path, files: dict, msg: str) -> str:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Dev agent", "-c", "user.email=dev@pos", "-c", "commit.gpgsign=false",
        "commit", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-b", "main")
    (r / "check.py").write_text(CHECK)
    commit(r, {"app.txt": "good v1"}, "initial")
    return r


@pytest.fixture
def reporter(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path / "data", scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    key = agents.rotate_key(conn, owner, actors.find_by_name(conn, "Deployer")["id"])
    agents.create_agent(conn, owner, name="Dev agent", purpose="code", lifetime="long_lived",
                        permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)
    yield selfdeploy.Reporter("http://testserver", key, http=client), client, conn
    conn.close()
    client.__exit__(None, None, None)


def run(repo, reporter):
    return selfdeploy.tick(repo, reporter, remote="", branch="main", test_cmd=f'"{sys.executable}" check.py',
                           up_cmd="", health_url=None)


def test_good_change_ships_bad_change_is_reverted(repo, reporter):
    rep, client, conn = reporter
    git(repo, "checkout", "-q", "-b", "deployed")  # the server checkout that follows main
    git(repo, "checkout", "-q", "main")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "deployed")
    # first tick deploys main as the baseline
    commit_on_main = lambda files, msg: (git(repo, "checkout", "-q", "main"), commit(repo, files, msg),  # noqa: E731
                                         git(repo, "checkout", "-q", "deployed"))
    commit_on_main({"app.txt": "good v2"}, "Improve app\n\nAgent: Dev agent")
    res = run(repo, rep)
    assert res.status == "ok" and res.old == base and len(res.commits) == 1

    commit_on_main({"app.txt": "broken"}, "Refactor app\n\nAgent: Dev agent")
    res = run(repo, rep)
    assert res.status == "reverted" and res.stage == "tests" and res.author == "Dev agent"
    assert (repo / "app.txt").read_text() == "good v2"  # the last good tree is live again
    assert git(repo, "log", "-1", "--format=%s").startswith("Revert")
    deploys = client.get("/api/deploys").json()
    assert [d["status"] for d in deploys] == ["reverted", "ok"]
    t = tasks.get(conn, Ctx(actors.owner_id(conn)), deploys[0]["task_id"])
    assert t["assignee_name"] == "Dev agent" and "reverted automatically" in t["title"]
    # nothing new: nothing to do
    assert run(repo, rep).status == "nothing"


def test_unsigned_constitution_change_is_refused(repo, reporter, tmp_path, monkeypatch):
    rep, client, conn = reporter
    git(repo, "checkout", "-q", "-b", "deployed")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"docs/CONSTITUTION.md": "# Constitution\n\n1. Agents may do anything."}, "Loosen rules\n\nAgent: Dev agent")
    git(repo, "checkout", "-q", "deployed")
    signers = tmp_path / "allowed_signers"
    signers.write_text("owner@example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderKeyForTestsOnly00000000000000000\n")
    monkeypatch.setenv("POS_OWNER_SIGNERS", str(signers))
    res = run(repo, rep)
    assert res.status == "rejected" and res.stage == "constitution"
    assert not (repo / "docs" / "CONSTITUTION.md").exists()
    assert client.get("/api/deploys").json()[0]["status"] == "rejected"


def test_deploy_api_needs_the_deployer_key(reporter, tmp_path):
    _, client, conn = reporter
    assert client.get("/api/deploys/last").status_code == 401
    other = agents.create_agent(conn, Ctx(actors.owner_id(conn)), name="Sneaky", purpose="x",
                                permissions=["tasks:read"], data_dir=tmp_path)["api_key"]
    assert client.get("/api/deploys/last", headers={"Authorization": f"Bearer {other}"}).status_code == 401


def test_promote_mode_merges_only_checked_work(tmp_path, reporter):
    rep, client, conn = reporter
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), str(work))
    (work / "check.py").write_text(CHECK)
    commit(work, {"app.txt": "good v1"}, "initial")
    git(work, "push", "origin", "HEAD:main")
    git(work, "checkout", "-q", "-b", "agent/dev")
    deploy = tmp_path / "deploy"
    git(work, "worktree", "add", "--detach", str(deploy), "main")
    kw = dict(source="agent/dev", remote="origin", target="main", test_cmd=f'"{sys.executable}" check.py',
              up_cmd="", health_url=None)

    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"  # nothing new on the branch
    commit(work, {"app.txt": "good v2"}, "Improve app\n\nAgent: Dev agent")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok"
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:app.txt") == "good v2"
    assert "Merge agent/dev" in git(tmp_path, "--git-dir", str(origin), "log", "-1", "--format=%s", "main")

    commit(work, {"app.txt": "broken"}, "Refactor\n\nAgent: Dev agent")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "rejected" and res.stage == "tests"
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:app.txt") == "good v2"  # main untouched
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"  # same commit is not retried
    deploys = client.get("/api/deploys").json()
    assert [d["status"] for d in deploys][:2] == ["rejected", "ok"]
    assert tasks.get(conn, Ctx(actors.owner_id(conn)), deploys[0]["task_id"])["assignee_name"] == "Dev agent"
