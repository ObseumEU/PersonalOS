import os
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


def test_promote_moves_the_mirror_the_agents_fetch_and_says_when_it_cannot(tmp_path, reporter):
    """T-730: the agents fetch main from the deployer checkout's .git (remote `deployer`); the promote
    push moved only origin/main, so their deployer/main stood on an old commit (ce70637)."""
    rep, client, conn = reporter
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"  # the deployer checkout: its .git is the agents' read-only mirror
    git(tmp_path, "clone", str(origin), str(work))
    (work / "check.py").write_text(CHECK)
    first = commit(work, {"app.txt": "good v1"}, "initial")
    git(work, "push", "origin", "HEAD:main")
    git(work, "checkout", "-q", "-b", "agent/dev")
    deploy = tmp_path / "deploy"
    git(work, "worktree", "add", "--detach", str(deploy), "main")
    agent = tmp_path / "agent"
    git(tmp_path, "clone", "-q", "--bare", str(work / ".git"), str(agent))
    kw = dict(source="agent/dev", remote="origin", target="main", test_cmd=f'"{sys.executable}" check.py',
              up_cmd="", health_url=None)

    # A: after a merge the mirror's main is the merge, and an agent's fetch gets it
    commit(work, {"app.txt": "good v2"}, "Improve app\n\nAgent: Software Engineer")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok"
    assert git(work, "rev-parse", "refs/heads/main") == res.new == git(work, "rev-parse", "origin/main")
    git(agent, "fetch", "-q", str(work / ".git"), "main")
    assert git(agent, "rev-parse", "FETCH_HEAD") == res.new
    assert selfdeploy.mirror_check(deploy, "origin", "main", rep.last_good()) == (
        True, f"deployer {res.new[:10]}, mirror {res.new[:10]}, prod {res.new[:10]}")

    # a mirror left behind (main moved elsewhere) catches up on the next tick, even with nothing to promote
    git(work, "update-ref", "refs/heads/main", first)
    assert not selfdeploy.mirror_check(deploy, "origin", "main", res.new)[0]
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"
    assert git(work, "rev-parse", "refs/heads/main") == res.new

    # B: the mirror cannot follow (diverged): never forced, the error is in the deploy log, the check fails
    git(work, "update-ref", "refs/heads/main",
        git(work, "-c", "user.name=Software Engineer", "-c", "user.email=dev@pos", "commit-tree", f"{first}^{{tree}}", "-p", first, "-m", "stray"))
    commit(work, {"app.txt": "good v3"}, "Polish app\n\nAgent: Software Engineer")
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok" and "mirror: refs/heads/main" in res.log and "not an ancestor" in res.log
    assert "mirror: refs/heads/main" in client.get("/api/deploys").json()[0]["log"]
    ok, line = selfdeploy.mirror_check(deploy, "origin", "main", res.new)
    assert not ok and f"deployer {res.new[:10]}" in line


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


def test_a_merge_rejection_is_retried_only_once_it_merges_cleanly(tmp_path):
    import json

    state = tmp_path / ".pos-promote-state"
    assert selfdeploy._should_try(state, "t1", "b1")
    selfdeploy._remember(state, "t1", "b1", "merge", ["a.py"])
    assert not selfdeploy._should_try(state, "t1", "b1")      # same tip, same main: not again
    assert not selfdeploy._should_try(state, "t1", "b2")      # main moved, nobody checked: parked
    assert not selfdeploy._should_try(state, "t1", "b2", lambda t, b: False)  # still conflicting: parked
    assert selfdeploy._should_try(state, "t1", "b2", lambda t, b: True)       # main resolved it: try again
    selfdeploy._remember(state, "t1", "b1", "tests")
    assert not selfdeploy._should_try(state, "t1", "b2")      # failed tests need a new commit
    assert selfdeploy._should_try(state, "t2", "b1")
    assert json.loads(state.read_text())["stage"] == "tests"


def test_refusals_of_one_branch_are_one_task_for_the_engineer_never_the_owner(reporter, tmp_path):
    """T-184..T-196: every refused tip was a new task, and the archived Dev agent's landed on the owner."""
    rep, client, conn = reporter
    owner = Ctx(actors.owner_id(conn))
    dev = agents.create_agent(conn, owner, name="Dev agent", purpose="old", lifetime="long_lived",
                              permissions=["tasks:read"], data_dir=tmp_path)["agent"]["id"]
    agents.archive(conn, owner, dev, "reorganisation")
    se = actors.find_by_name(conn, "Software Engineer")["id"]

    def refuse(tip: str, author: str) -> dict:
        return rep.report(selfdeploy.Result("a" * 40, tip * 40, "rejected", stage="merge", log="CONFLICT (content)",
                                            author=author, commits=["x", "y"], branch="dev/agent/dev"))

    first = refuse("1", "Dev agent")
    t = tasks.get(conn, owner, tasks.parse_id(first["task"]))
    assert t["assignee_id"] == se  # the archived author's successor, not the owner
    assert "Branch: dev/agent/dev" in t["notes"]
    tasks.claim(conn, Ctx(se, via="mcp"), t["id"])
    tasks.complete(conn, Ctx(se, via="mcp"), t["id"], "rebased")  # handed in, then refused again
    conn.commit()  # (the MCP session commits; the deployer reports through the API's own connection)
    assert refuse("2", "Software Engineer")["task"] == first["task"]
    assert refuse("3", "David Rosko")["task"] == first["task"]  # a person's commit: still the branch's owner
    t = tasks.get(conn, owner, t["id"])
    assert t["status"] == "next" and t["assignee_id"] == se
    again = [c for c in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (t["id"],))
             if c["body"].startswith("Again:")]
    assert len(again) == 2
    open_ = conn.execute("SELECT COUNT(*) FROM tasks WHERE source = 'deployer' AND status != 'done'").fetchone()[0]
    assert open_ == 1
    # another branch is another task; an unknown author there goes to the engineer too
    other = rep.report(selfdeploy.Result("a" * 40, "4" * 40, "rejected", stage="tests", author="someone",
                                         branch="main"))
    assert other["task"] != first["task"]
    assert tasks.get(conn, owner, tasks.parse_id(other["task"]))["assignee_id"] == se
    assert not conn.execute("SELECT 1 FROM tasks WHERE source = 'deployer' AND assignee_id = ?",
                            (owner.actor_id,)).fetchone()


def _promote_setup(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"  # the agent's clone, on its branch
    git(tmp_path, "clone", str(origin), str(work))
    (work / "check.py").write_text(CHECK)
    commit(work, {"app.txt": "good v1\n"}, "initial")
    git(work, "push", "origin", "HEAD:main")
    git(work, "checkout", "-q", "-b", "agent/dev")
    other = tmp_path / "other"  # someone else merging to main meanwhile
    git(tmp_path, "clone", str(origin), str(other))
    deploy = tmp_path / "deploy"
    git(work, "worktree", "add", "--detach", str(deploy), "main")
    kw = dict(source="agent/dev", remote="origin", target="main", test_cmd=f'"{sys.executable}" check.py',
              up_cmd="", health_url=None)
    return origin, work, other, deploy, kw


def _on_main(other, files, msg):
    git(other, "pull", "-q", "--ff-only", "origin", "main")
    sha = commit(other, files, msg)
    git(other, "push", "-q", "origin", "HEAD:main")
    return sha


def test_a_conflict_is_one_task_and_is_never_retried_until_a_new_commit(tmp_path, reporter):
    """10 of 20 deploys were refused on conflicts, the same one 8 times: main kept moving and the
    deployer retried the same tip each time. Now: one automatic rebase, one task, then parked."""
    rep, client, conn = reporter
    origin, work, other, deploy, kw = _promote_setup(tmp_path)
    commit(work, {"app.txt": "good from the branch\n"}, "Branch change\n\nAgent: Software Engineer")
    _on_main(other, {"app.txt": "good from main\n"}, "Main change")

    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "rejected" and res.stage == "merge"
    assert "app.txt" in res.reason and "rebase" in res.log and "<<<<<<<" in res.log
    deploys = client.get("/api/deploys").json()
    assert len(deploys) == 1 and deploys[0]["reason"].startswith("merge conflict in app.txt")
    t = tasks.get(conn, Ctx(actors.owner_id(conn)), deploys[0]["task_id"])
    assert t["title"].startswith("Rebase agent/dev onto main") and t["assignee_name"] == "Software Engineer"
    assert "app.txt" in t["notes"] and "<<<<<<<" in t["notes"]

    # main moves on (unrelated changes) three times: silently re-checked, never another attempt or report
    for i in range(3):
        _on_main(other, {f"other{i}.txt": "x"}, f"Unrelated {i}")
        again = selfdeploy.promote_tick(deploy, rep, **kw)
        assert again.status == "nothing" and again.stage == "parked"
    assert len(client.get("/api/deploys").json()) == 1

    # a new commit that still conflicts: tried once more, the same task gets a comment
    commit(work, {"app.txt": "good from the branch, again\n"}, "Another try\n\nAgent: Software Engineer")
    assert selfdeploy.promote_tick(deploy, rep, **kw).stage == "merge"
    deploys = client.get("/api/deploys").json()
    assert len(deploys) == 2 and deploys[0]["task_id"] == deploys[1]["task_id"]
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source = 'deployer' AND status != 'done'"
                        ).fetchone()[0] == 1
    health = client.get("/api/deploys/health").json()
    assert health["attempts"] == 2 and health["reject_rate"] == 1.0 and health["repeats"] == []
    assert health["by_stage"] == {"merge": 2}

    # main takes the branch's side: the parked tip merges cleanly now and ships
    _on_main(other, {"app.txt": "good from the branch, again\n"}, "Take the branch's version")
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "ok"


def test_a_conflict_that_a_rebase_resolves_ships_without_a_task(tmp_path, reporter):
    """The branch's first commit is already on main (an earlier automatic rebase promoted a copy)
    and main changed the same lines since: a merge conflicts, a rebase drops the copy and is clean."""
    rep, client, conn = reporter
    origin, work, other, deploy, kw = _promote_setup(tmp_path)
    first = commit(work, {"app.txt": "good v2\n"}, "Improve\n\nAgent: Software Engineer")
    git(other, "pull", "-q", "--ff-only", "origin", "main")
    git(other, "fetch", "-q", str(work), "agent/dev")
    # GIT_COMMITTER_* from the environment beat `-c user.name`: in the same second the copy came out
    # identical to `first` (a shared commit, no conflict). Its own committer and date make it a real copy.
    deployer = {**os.environ, "GIT_COMMITTER_NAME": "PersonalOS Deployer", "GIT_COMMITTER_EMAIL": "d@pos",
                "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"}
    git(other, "cherry-pick", first, env=deployer)
    assert git(other, "rev-parse", "HEAD") != first
    git(other, "push", "-q", "origin", "HEAD:main")
    _on_main(other, {"app.txt": "good v3\n"}, "Improve more")
    commit(work, {"notes.txt": "new\n"}, "Add notes\n\nAgent: Software Engineer")

    assert not selfdeploy.merges_cleanly(deploy, git(work, "rev-parse", "HEAD"),
                                         git(other, "rev-parse", "HEAD"))
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok" and "automatic rebase" in res.log
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:app.txt") == "good v3"
    assert git(tmp_path, "--git-dir", str(origin), "show", "main:notes.txt") == "new"
    assert "rebased onto main automatically" in git(tmp_path, "--git-dir", str(origin), "log", "-1",
                                                    "--format=%B", "main")
    assert git(work, "rev-parse", "HEAD") == git(work, "rev-parse", "agent/dev")  # the branch is untouched
    assert not conn.execute("SELECT 1 FROM tasks WHERE source = 'deployer'").fetchone()
    assert "pos-rebase-" not in git(deploy, "worktree", "list")  # the scratch worktree is gone
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"  # the same tip is not promoted twice


def test_a_branch_behind_main_is_rebased_promoted_and_then_contained_in_main(tmp_path, reporter):
    """Deploys 27-30: agent/dev never took main back (its origin was days behind), the deployer promoted
    rebased copies, and every later commit on the branch conflicted with those copies again. Now a
    branch behind main is rebased before the merge (no conflict needed), main records the agent's own
    tip as merged, and the next commit on the still stale branch merges cleanly."""
    rep, client, conn = reporter
    origin, work, other, deploy, kw = _promote_setup(tmp_path)
    lines = "".join(f"{i}\n" for i in range(1, 8))
    _on_main(other, {"lines.txt": lines}, "Lines")
    git(work, "pull", "-q", "--ff-only", "origin", "main")
    first = commit(work, {"lines.txt": lines.replace("4\n", "4 branch\n")}, "Branch: line 4\n\nAgent: Software Engineer")
    _on_main(other, {"lines.txt": lines.replace("2\n", "2 main\n")}, "Main: line 2")  # behind, no conflict

    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "ok" and "automatic rebase" in res.log
    show = lambda path: git(tmp_path, "--git-dir", str(origin), "show", f"main:{path}")  # noqa: E731
    assert show("lines.txt") == lines.replace("2\n", "2 main\n").replace("4\n", "4 branch\n").strip()
    assert "rebased onto main automatically" in git(tmp_path, "--git-dir", str(origin), "log", "-1", "--format=%B",
                                                    "main")
    parents = git(tmp_path, "--git-dir", str(origin), "log", "-1", "--format=%P", "main").split()
    assert parents[1] == first  # the agent's own commit, not a rebased copy: main contains agent/dev
    assert git(work, "rev-parse", "agent/dev") == first  # the agent's clone is never written to

    # main changes the branch's line again; the agent, still stale, commits something else
    _on_main(other, {"lines.txt": show("lines.txt").replace("4 branch", "4 main") + "\n"}, "Main: line 4")
    commit(work, {"notes.txt": "new\n"}, "Notes\n\nAgent: Software Engineer")
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "ok"  # a rebased copy would conflict here
    assert "4 main" in show("lines.txt") and show("notes.txt") == "new"
    assert not conn.execute("SELECT 1 FROM tasks WHERE source = 'deployer'").fetchone()
    assert [d["status"] for d in client.get("/api/deploys").json()] == ["ok", "ok"]

    # agent/dev takes main back as a fast-forward: nothing of its own history to replay
    git(work, "fetch", "-q", "origin", "main")
    git(work, "merge", "-q", "--ff-only", "origin/main")
    assert git(work, "rev-parse", "agent/dev") == git(tmp_path, "--git-dir", str(origin), "rev-parse", "main")
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"


def test_a_rebase_conflict_is_one_task_and_the_tip_stays_parked(tmp_path, reporter):
    rep, client, conn = reporter
    origin, work, other, deploy, kw = _promote_setup(tmp_path)
    commit(work, {"app.txt": "good from the branch\n", "a.txt": "a\n"}, "Branch\n\nAgent: Software Engineer")
    _on_main(other, {"app.txt": "good from main\n"}, "Main")

    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.stage == "merge" and "automatic rebase onto main" in res.log and "<<<<<<<" in res.log
    assert "git rebase deployer/main" in res.log
    for i in range(2):  # the same tip: never a second attempt, report or task, whether main moves or not
        assert selfdeploy.promote_tick(deploy, rep, **kw).status == "nothing"
        _on_main(other, {f"other{i}.txt": "x"}, f"Unrelated {i}")
    assert len(client.get("/api/deploys").json()) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source = 'deployer'").fetchone()[0] == 1
    assert "pos-rebase-" not in git(deploy, "worktree", "list")


def test_reason_of_a_rejection_and_the_repeat_count(reporter):
    rep, client, conn = reporter
    tests_log = "....F\nFAILED tests/test_x.py::test_a - assert 1 == 2\nFAILED tests/test_x.py::test_b\n2 failed"
    r = selfdeploy.Result("a" * 40, "b" * 40, "rejected", stage="tests", log=tests_log, branch="dev/agent/dev")
    assert selfdeploy.reason_of(r) == ("tests: FAILED tests/test_x.py::test_a - assert 1 == 2 "
                                       "(+1 more: tests/test_x.py::test_b)")
    rep.report(r)
    rep.report(r)  # the same commit refused at the same stage twice: a repeat
    rep.report(selfdeploy.Result("a" * 40, "c" * 40, "ok", branch="dev/agent/dev"))
    h = client.get("/api/deploys/health").json()
    assert h["attempts"] == 3 and h["ok"] == 1 and h["reject_rate"] == round(2 / 3, 3)
    assert h["repeats"] == [{"sha": "b" * 10, "stage": "tests", "times": 2}] and h["repeat_attempts"] == 1
    assert h["top_reasons"][0]["count"] == 2 and h["last_ok_at"]
def test_a_change_to_the_deployer_recreates_it_from_a_helper_container(repo, monkeypatch):
    """T-305: the deployer never stops itself; a detached container from its own image runs the compose."""
    base = git(repo, "rev-parse", "HEAD")
    web = commit(repo, {"web/src/App.tsx": "x"}, "web only")
    assert not selfdeploy.deployer_changed(repo, base, web)
    own = commit(repo, {"backend/src/pos/selfdeploy.py": "x"}, "deployer code")
    assert selfdeploy.deployer_changed(repo, web, own)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = "personalos-deployer\n" if cmd[1] == "inspect" else "abc123\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setenv("HOSTNAME", "deployer-ctr")
    monkeypatch.setattr(selfdeploy.subprocess, "run", fake_run)
    ok, _ = selfdeploy.restart_self("docker compose up -d --build --no-deps deployer", repo)
    assert ok
    assert calls[0][-1] == "deployer-ctr"
    run = calls[1]
    assert run[:4] == ["docker", "run", "-d", "--rm"] and "personalos-deployer" in run
    assert f"{repo}:{repo}" in run and run[-1] == "sleep 5 && docker compose up -d --build --no-deps deployer"


def test_the_deployer_exits_cleanly_on_sigterm():
    """T-305: as PID 1 Python ignores SIGTERM without a handler; docker stop then ended in exit 137."""
    with pytest.raises(SystemExit) as e:
        selfdeploy._stop(15, None)
    assert e.value.code == 0


def test_new_commits_that_leave_the_conflict_as_it_was_stay_parked_silently(tmp_path, reporter):
    """Deploys 46-51: six new tips on agent/dev, the same conflict in the same file, six refusals.
    A new commit that does not touch the conflicting files changes nothing: parked, no report."""
    rep, client, conn = reporter
    origin, work, other, deploy, kw = _promote_setup(tmp_path)
    commit(work, {"app.txt": "good from the branch\n"}, "Branch change\n\nAgent: Software Engineer")
    _on_main(other, {"app.txt": "good from main\n"}, "Main change")
    assert selfdeploy.promote_tick(deploy, rep, **kw).stage == "merge"
    for i in range(3):
        commit(work, {f"more{i}.txt": "x\n"}, f"More work {i}\n\nAgent: Software Engineer")
        again = selfdeploy.promote_tick(deploy, rep, **kw)
        assert again.status == "nothing" and again.stage == "parked"
    assert len(client.get("/api/deploys").json()) == 1
    # a commit that works on the conflicting file is worth one more attempt
    commit(work, {"app.txt": "good from the branch, again\n"}, "Resolve?\n\nAgent: Software Engineer")
    assert selfdeploy.promote_tick(deploy, rep, **kw).stage == "merge"
    deploys = client.get("/api/deploys").json()
    assert len(deploys) == 2 and deploys[0]["reason"] == "merge conflict in app.txt"


def test_a_refused_tip_and_base_is_never_tried_again_even_after_another_attempt(tmp_path):
    state = tmp_path / "state"
    selfdeploy._remember(state, "t1", "b1", "tests")
    selfdeploy._remember(state, "t2", "b1", "tests")  # the branch moved to t2 ...
    assert not selfdeploy._should_try(state, "t1", "b1")  # ... and back to t1 (a reset): still refused
    assert selfdeploy._should_try(state, "t1", "b2")  # main moved: a new pair
    selfdeploy._remember(state, "t3", "b1", "ok")
    assert not selfdeploy._should_try(state, "t2", "b1")  # a success forgets no refusal


def test_a_tests_rejection_names_every_failing_test_and_tests_get_a_git_identity(tmp_path, monkeypatch):
    """T-746: the Software Engineer needs the failing tests' names, not the tail of the output; and
    a test that commits must not depend on the machine's global git identity (deploy 52)."""
    _no_identity(tmp_path, monkeypatch)
    script = tmp_path / "t.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.run(['git', 'init', '-q', 'r'], check=True)\n"
        "subprocess.run(['git', '-C', 'r', 'commit', '-q', '--allow-empty', '-m', 'x'], check=True)\n"
        "print('x' * 9000)\n"
        "print('FAILED tests/test_a.py::test_one - boom')\n"
        "print('ERROR tests/test_b.py::test_two')\n"
        "sys.exit(1)\n")
    ok, log = selfdeploy.run_tests(f'"{sys.executable}" t.py', tmp_path)
    assert not ok
    assert log.startswith("Failing tests (2):\n  - tests/test_a.py::test_one\n  - tests/test_b.py::test_two")
    assert "CalledProcessError" not in log and "tell me who you are" not in log
    assert selfdeploy.failing_tests(log) == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]


def test_a_refusal_task_keeps_the_conflicting_files_however_long_the_log(reporter):
    rep, client, conn = reporter
    log = "CONFLICT: x. Conflicting files:\n  - a.py\n  - b.py\n\nHunks:\n" + "h" * 9000
    rep.report(selfdeploy.Result("a" * 40, "b" * 40, "rejected", stage="merge", log=log, branch="agent/dev",
                                 author="Software Engineer", reason="merge conflict in a.py, b.py"))
    d = client.get("/api/deploys").json()[0]
    t = tasks.get(conn, Ctx(actors.owner_id(conn)), d["task_id"])
    assert "  - a.py\n  - b.py" in t["notes"] and "Reason: merge conflict in a.py, b.py" in t["notes"]
