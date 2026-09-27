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
    git(repo, "-c", "user.name=Software Engineer", "-c", "user.email=dev@pos", "-c", "commit.gpgsign=false",
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
    agents.create_agent(conn, owner, name="Software Engineer", purpose="code", lifetime="long_lived",
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
    commit_on_main({"app.txt": "good v2"}, "Improve app\n\nAgent: Software Engineer")
    res = run(repo, rep)
    assert res.status == "ok" and res.old == base and len(res.commits) == 1

    commit_on_main({"app.txt": "broken"}, "Refactor app\n\nAgent: Software Engineer")
    res = run(repo, rep)
    assert res.status == "reverted" and res.stage == "tests" and res.author == "Software Engineer"
    assert (repo / "app.txt").read_text() == "good v2"  # the last good tree is live again
    assert git(repo, "log", "-1", "--format=%s").startswith("Revert")
    deploys = client.get("/api/deploys").json()
    assert [d["status"] for d in deploys] == ["reverted", "ok"]
    t = tasks.get(conn, Ctx(actors.owner_id(conn)), deploys[0]["task_id"])
    assert t["assignee_name"] == "Software Engineer" and "reverted automatically" in t["title"]
    # nothing new: nothing to do
    assert run(repo, rep).status == "nothing"


def test_unsigned_constitution_change_is_refused(repo, reporter, tmp_path, monkeypatch):
    rep, client, conn = reporter
    git(repo, "checkout", "-q", "-b", "deployed")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"docs/CONSTITUTION.md": "# Constitution\n\n1. Agents may do anything."}, "Loosen rules\n\nAgent: Software Engineer")
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
    commit(work, {"app.txt": "good v2"}, "Improve app\n\nAgent: Software Engineer")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok"
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:app.txt") == "good v2"
    assert "Merge agent/dev" in git(tmp_path, "--git-dir", str(origin), "log", "-1", "--format=%s", "main")

    commit(work, {"app.txt": "broken"}, "Refactor\n\nAgent: Software Engineer")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "rejected" and res.stage == "tests"
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:app.txt") == "good v2"  # main untouched
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"  # same commit is not retried
    deploys = client.get("/api/deploys").json()
    assert [d["status"] for d in deploys][:2] == ["rejected", "ok"]
    assert tasks.get(conn, Ctx(actors.owner_id(conn)), deploys[0]["task_id"])["assignee_name"] == "Software Engineer"


def test_promote_rolls_production_back_when_health_fails_and_respects_the_kill_switch(tmp_path, reporter,
                                                                                         monkeypatch):
    from pos import killswitch

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
    live = tmp_path / "live.txt"
    up = f'"{sys.executable}" -c "import shutil; shutil.copy(\'app.txt\', r\'{live}\')"'
    # "good but slow" passes the tests, but the running service is not healthy on it
    monkeypatch.setattr(selfdeploy, "healthy", lambda url, wait_s=90: ("slow" not in live.read_text(), "timeout"))
    kw = dict(source="agent/dev", remote="origin", target="main", test_cmd=f'"{sys.executable}" check.py',
              up_cmd=up, health_url="http://web/api/health")
    commit(work, {"app.txt": "good but slow"}, "Speed up\n\nAgent: Software Engineer")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "rejected" and res.stage == "health" and "rolled back" in res.log
    assert live.read_text() == "good v1"  # production is on main's last good version again
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:app.txt") == "good v1"

    assert rep.frozen() is False
    killswitch.freeze(conn, Ctx(actors.owner_id(conn)), "test")
    conn.commit()
    assert rep.frozen() is True


def test_restore_owner_gives_root_owned_files_back(tmp_path, monkeypatch):
    """The deployer runs as root: after a tick, everything it wrote in .git (and, after a
    deploy, the working tree, not node_modules or data) goes back to the checkout's owner."""
    import os

    repo = tmp_path / "co"
    (repo / ".git" / "objects" / "ab").mkdir(parents=True)
    (repo / ".git" / "objects" / "ab" / "cdef").write_text("x")
    (repo / "backend").mkdir()
    (repo / "backend" / "a.py").write_text("x")
    (repo / "web" / "node_modules" / "m").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "data" / "pos.db").write_text("x")
    owners = {str(repo): 1000}
    changed = []
    monkeypatch.setattr(selfdeploy, "_is_root", lambda: True)
    monkeypatch.setattr(selfdeploy, "_owner", lambda p: (owners.get(str(p), 0), 1000))
    monkeypatch.setattr(selfdeploy, "_chown", lambda p, u, g: (changed.append(os.path.relpath(p, repo)),
                                                               owners.__setitem__(str(p), u)))
    monkeypatch.setattr(selfdeploy, "git", lambda *a: str(repo / ".git"))
    assert selfdeploy.restore_owner(repo, tree=False) == 4  # .git, objects, ab, cdef
    assert all(c.startswith(".git") for c in changed)
    changed.clear()
    assert selfdeploy.restore_owner(repo, tree=True) == 3
    assert {c.replace(os.sep, "/") for c in changed} == {"backend", "backend/a.py", "web"}
    assert selfdeploy.restore_owner(repo, tree=True) == 0  # nothing left to give back
    monkeypatch.setattr(selfdeploy, "_is_root", lambda: False)
    owners.clear()
    owners[str(repo)] = 1000
    assert selfdeploy.restore_owner(repo) == 0  # not root: a no-op


def test_a_failed_rollback_is_one_revert_and_one_ticket_for_the_sre(repo, reporter, tmp_path):
    rep, client, conn = reporter
    owner = Ctx(actors.owner_id(conn))
    sre = agents.create_agent(conn, owner, name="SRE", purpose="ops", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    git(repo, "checkout", "-q", "-b", "deployed")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"app.txt": "good v2"}, "Improve\n\nAgent: Software Engineer")
    git(repo, "checkout", "-q", "deployed")
    kw = dict(remote="", branch="main", test_cmd="", health_url=None)
    assert selfdeploy.tick(repo, rep, up_cmd="", **kw).status == "ok"
    git(repo, "checkout", "-q", "main")
    commit(repo, {"app.txt": "bad"}, "Break\n\nAgent: Software Engineer")
    git(repo, "checkout", "-q", "deployed")
    first = selfdeploy.tick(repo, rep, up_cmd="exit 1", **kw)  # the build fails, and so does the redeploy
    assert first.status == "error" and first.reverted_sha
    for _ in range(3):  # later ticks: the revert is where main is; nothing to deploy, no new revert
        assert selfdeploy.tick(repo, rep, up_cmd="exit 1", **kw).status == "nothing"
    assert git(repo, "rev-parse", "main") == first.reverted_sha
    tickets = conn.execute("SELECT * FROM tasks WHERE title = 'Deploy failed and could not roll back by itself'"
                           ).fetchall()
    assert len(tickets) == 1 and tickets[0]["assignee_id"] == sre
    owner_p1 = conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND priority = 1",
                            (actors.owner_id(conn),)).fetchone()[0]
    assert owner_p1 == 0


def test_the_deployer_never_reverts_its_own_revert(repo, reporter):
    rep, client, conn = reporter
    git(repo, "checkout", "-q", "-b", "deployed")
    git(repo, "checkout", "-q", "main")
    good = git(repo, "rev-parse", "HEAD")
    commit(repo, {"app.txt": "bad"}, "Break\n\nAgent: Software Engineer")
    bad = git(repo, "rev-parse", "HEAD")
    revert = selfdeploy.revert_to(repo, good, bad, "tests failed", "", "main")
    git(repo, "checkout", "-q", "deployed")
    res = selfdeploy.deploy_range(repo, bad, revert, test_cmd="", up_cmd="exit 1", health_url=None, remote="",
                                  branch="main")
    assert res.status == "error" and res.reverted_sha is None and "not reverting" in res.log
    assert git(repo, "rev-parse", "main") == revert


def _no_identity(tmp_path, monkeypatch):
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
              "DEPLOY_GIT_NAME", "DEPLOY_GIT_EMAIL"):
        monkeypatch.delenv(k, raising=False)


def test_promote_merges_without_a_git_identity_and_retries_an_old_rejection(tmp_path, reporter, monkeypatch):
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
    tip = commit(work, {"app.txt": "good v2"}, "Improve\n\nAgent: Software Engineer")
    # the old deployer stored the tip it failed to merge (no identity) and never tried it again
    (deploy / ".pos-promote-state").write_text(tip)
    _no_identity(tmp_path, monkeypatch)
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok"
    assert git(tmp_path, "--git-dir", str(origin), "log", "-1", "--format=%cn", "main") == "PersonalOS Deployer"
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"


def test_a_merge_rejection_is_retried_once_main_moves(tmp_path):
    import json

    state = tmp_path / ".pos-promote-state"
    assert selfdeploy._should_try(state, "t1", "b1")
    selfdeploy._remember(state, "t1", "b1", "merge")
    assert not selfdeploy._should_try(state, "t1", "b1")      # same tip, same main: not again
    assert selfdeploy._should_try(state, "t1", "b2")          # main moved: a conflict may be gone
    selfdeploy._remember(state, "t1", "b1", "tests")
    assert not selfdeploy._should_try(state, "t1", "b2")      # failed tests need a new commit
    assert selfdeploy._should_try(state, "t2", "b1")
    assert json.loads(state.read_text())["stage"] == "tests"
