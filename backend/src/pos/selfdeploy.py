"""The deployer: agents change the platform, the platform checks itself
(AGENTS-SPEC 6, step 6).

Agents (the Dev agent first) merge their work to main, like we do. The
deployer runs where the server's checkout lives and, for every new range of
commits on main:

1. constitution check: commits touching protected paths must be signed by the
   owner (pos.guard.gitcheck); otherwise the range is refused;
2. tests (DEPLOY_TEST_CMD);
3. build and start (DEPLOY_UP_CMD);
4. health check (DEPLOY_HEALTH_URL must answer {"status": "ok"}).

If any step fails, it reverts the range itself with one new commit whose tree
is the last good one (no force-push, no rewritten history), deploys that
again, and reports to PersonalOS, which gives the author a task with the log.

    python -m pos.selfdeploy --repo /srv/personalos --once
    python -m pos.selfdeploy --repo /srv/personalos --watch 60

Environment: POS_URL, POS_DEPLOYER_KEY (the Deployer member's key),
DEPLOY_TEST_CMD, DEPLOY_UP_CMD, DEPLOY_HEALTH_URL, DEPLOY_BRANCH (main),
DEPLOY_REMOTE (origin).
"""

import argparse
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .guard import gitcheck

DEFAULT_TEST = "cd backend && python -m pytest -q"
DEFAULT_UP = "docker compose up -d --build api web"


@dataclass
class Result:
    old: str
    new: str
    status: str  # ok | reverted | rejected | error | nothing
    stage: str = ""
    log: str = ""
    author: str = ""
    reverted_sha: str | None = None
    commits: list[str] = field(default_factory=list)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def sh(cmd: str, repo: Path, timeout: int = 1800) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=repo, shell=True, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode == 0, (p.stdout + p.stderr)[-6000:]


def healthy(url: str, wait_s: int = 90) -> tuple[bool, str]:
    deadline = time.monotonic() + wait_s
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=5)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True, "healthy"
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001 - not up yet
            last = str(e)
        time.sleep(3)
    return False, f"health check failed: {last}"


def author_of(repo: Path, old: str, new: str) -> str:
    """The member responsible: an `Agent: <name>` trailer wins, else the author name."""
    log = git(repo, "log", "--format=%an%x00%B%x01", f"{old}..{new}")
    for entry in log.split("\x01"):
        if not entry.strip():
            continue
        name, _, body = entry.partition("\x00")
        if m := re.search(r"^Agent:\s*(.+)$", body, re.MULTILINE):
            return m.group(1).strip()
        return name.strip()
    return ""


def revert_to(repo: Path, good: str, bad: str, reason: str, remote: str, branch: str) -> str:
    """One new commit on top of `bad` whose tree is `good`'s: undoes the whole
    range without rewriting history."""
    tree = git(repo, "rev-parse", f"{good}^{{tree}}")
    msg = (f"Revert {good[:10]}..{bad[:10]}: {reason}\n\n"
           f"Automatic revert by the PersonalOS deployer (AGENTS-SPEC 6).\n\nAgent: Deployer")
    sha = git(repo, "commit-tree", tree, "-p", bad, "-m", msg)
    git(repo, "merge", "--ff-only", sha)
    if remote:
        subprocess.run(["git", "push", remote, f"{sha}:{branch}"], cwd=repo, check=True, capture_output=True, text=True)
    elif git(repo, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        # Local mode: move the branch the deployer follows (a fast-forward, bad -> revert).
        git(repo, "update-ref", f"refs/heads/{branch}", sha, bad)
    return sha


def deploy_range(repo: Path, old: str, new: str, *, test_cmd: str, up_cmd: str, health_url: str | None,
                 remote: str = "origin", branch: str = "main") -> Result:
    commits = gitcheck.commits_in_range(repo, old, new)
    res = Result(old, new, "ok", author=author_of(repo, old, new), commits=commits)
    git(repo, "merge", "--ff-only", new)

    def fail(stage: str, log: str) -> Result:
        res.status, res.stage, res.log = "reverted", stage, log
        res.reverted_sha = revert_to(repo, old, new, f"{stage} failed", remote, branch)
        ok, up_log = sh(up_cmd, repo) if up_cmd else (True, "")
        if not ok:
            res.status, res.log = "error", f"{log}\n\n--- redeploy of the last good version failed ---\n{up_log}"
        return res

    check = gitcheck.check_range(repo, old, new)
    if check.problems:
        res.status = "rejected"
        detail = "; ".join(f"{p.sha[:10]} touches {', '.join(p.paths)} without the owner's signature"
                           for p in check.problems)
        out = fail("constitution", detail)
        out.status = "rejected" if out.status != "error" else out.status
        return out
    if check.unsigned_protected:
        res.log = "warning (signing not set up yet): " + "; ".join(
            f"{p.sha[:10]} changes {', '.join(p.paths)}" for p in check.unsigned_protected)
    if test_cmd:
        ok, log = sh(test_cmd, repo)
        if not ok:
            return fail("tests", log)
    if up_cmd:
        ok, log = sh(up_cmd, repo)
        if not ok:
            return fail("deploy", log)
    if health_url:
        ok, log = healthy(health_url)
        if not ok:
            return fail("health", log)
    return res


class Reporter:
    def __init__(self, url: str, key: str, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=url, timeout=30)
        self.auth = {"Authorization": f"Bearer {key}"}

    def last_good(self) -> str | None:
        r = self.http.get("/api/deploys/last", headers=self.auth)
        r.raise_for_status()
        return r.json().get("sha")

    def report(self, res: Result) -> dict:
        r = self.http.post("/api/deploys", headers=self.auth, json={
            "old_sha": res.old, "new_sha": res.new, "status": res.status, "stage": res.stage,
            "log": res.log[-8000:], "author": res.author, "reverted_sha": res.reverted_sha,
            "commits": len(res.commits),
        })
        r.raise_for_status()
        return r.json()


def tick(repo: Path, reporter: Reporter, *, remote: str, branch: str, test_cmd: str, up_cmd: str,
         health_url: str | None) -> Result:
    if remote:
        git(repo, "fetch", remote, branch)
        new = git(repo, "rev-parse", f"{remote}/{branch}")
    else:
        new = git(repo, "rev-parse", branch)
    old = reporter.last_good() or git(repo, "rev-parse", "HEAD")
    if old == new:
        return Result(old, new, "nothing")
    res = deploy_range(repo, old, new, test_cmd=test_cmd, up_cmd=up_cmd, health_url=health_url,
                       remote=remote, branch=branch)
    reporter.report(res)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", type=int, default=0, help="seconds between checks")
    a = ap.parse_args()
    reporter = Reporter(os.environ.get("POS_URL", "http://localhost:8000"), os.environ["POS_DEPLOYER_KEY"])
    kw = dict(remote=os.environ.get("DEPLOY_REMOTE", "origin"), branch=os.environ.get("DEPLOY_BRANCH", "main"),
              test_cmd=os.environ.get("DEPLOY_TEST_CMD", DEFAULT_TEST), up_cmd=os.environ.get("DEPLOY_UP_CMD", DEFAULT_UP),
              health_url=os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090/api/health"))
    while True:
        res = tick(Path(a.repo), reporter, **kw)
        if res.status != "nothing":
            print(f"{res.old[:10]}..{res.new[:10]}: {res.status} {res.stage}", flush=True)
        if a.once or not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
