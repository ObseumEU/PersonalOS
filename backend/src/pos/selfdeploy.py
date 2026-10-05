"""The deployer: agents change the platform, the platform checks itself
(AGENTS-SPEC 6, step 6).

Agents (the Dev agent first) merge their work to main, like we do. The
deployer runs where the server's checkout lives and, for every new range of
commits on main:

1. constitution check: commits touching protected paths must be signed by the
   owner (pos.guard.gitcheck); otherwise the range is refused; commits that
   touch shared/tools/ must also pass the tools guard review (pos.tools:
   no secrets, no undeclared outbound calls, no permission escalation);
2. tests (DEPLOY_TEST_CMD);
3. build and start (DEPLOY_UP_CMD);
4. health check (DEPLOY_HEALTH_URL must answer {"status": "ok"}).

If any step fails, it reverts the range itself with one new commit whose tree
is the last good one (no force-push, no rewritten history), deploys that
again, and reports to PersonalOS, which gives the author a task with the log.

    python -m pos.selfdeploy --repo /srv/personalos --once
    python -m pos.selfdeploy --repo /srv/personalos --watch 60

Promote mode (a PC, or anywhere agents should not touch main directly):
agents commit to a branch (agent/dev); the deployer merges it into the latest
main in its own worktree, runs the constitution check, the tests and a
staging stack with a health check, and only then pushes the merge to main.
On failure nothing reaches main and the author gets a task with the log.
After the push it also moves the checkout's own main (the agents fetch it read-only as
`deployer/main`); `--check-mirror` is the health check that the three hashes agree (T-730).

    python -m pos.selfdeploy --repo <deploy worktree> --promote-from agent/dev --watch 60

Environment: POS_URL, POS_DEPLOYER_KEY (the Deployer member's key),
DEPLOY_TEST_CMD, DEPLOY_UP_CMD, DEPLOY_HEALTH_URL, DEPLOY_BRANCH (main),
DEPLOY_REMOTE (origin), DEPLOY_GIT_NAME / DEPLOY_GIT_EMAIL (who the deployer's own merge and
revert commits are by; default "PersonalOS Deployer" when the checkout has no identity).
A revert that fails too is never reverted again (one attempt per range). DEPLOY_REQUIRE_REVIEW=1 (promote mode): the QA Reviewer
approves each new tip first (pos.deploy_review); until then the tick waits.

A branch that is behind main (promote mode) is rebased onto the current main before it is
promoted, once per tip, in a scratch worktree, not only after a merge conflict; never for commits
that touch protected paths, whose owner signatures a rebase would drop: those are merged as they
are. A clean rebase goes on through the tests like any merge, and main records it as a merge
of the agent's own tip (the rebase's tree, the original tip as the second parent, not the rebased
copies): main then contains agent/dev, the next tick rebases only what is new on the branch, and
the agent fast-forwards. Before, main got the rebased copies and agent/dev kept the originals, so
every later commit on the branch conflicted again with its own promoted history (deploys 27-30,
a branch 41 commits behind). The deployer never writes to agent/dev (the agent's clone is
mounted read-only and has the branch checked out) and never rewrites main; the agent brings the
branch up to date with `git fetch deployer main` and `git rebase deployer/main` (the deployer's
checkout; the clone's `origin` has no credentials and stayed days behind).
A conflict is never retried as such: the branch's owner gets one task (per branch) with the
conflicting files and hunks and the instruction to rebase, and the tip is parked: later ticks
only re-check it silently (git merge-tree) when main moves, and try it again only once it merges
cleanly or a new commit arrives. Every rejection carries a one-line reason (GET
/api/deploys/health sums them up).

DEPLOY_SELF_CMD: the compose command that recreates the deployer itself (e.g. `docker compose
… up -d --build --no-deps deployer`). After a deploy that changed the deployer's own code it runs
in a detached helper container, never inside the deployer, which it stops (T-305).
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import tools
from .guard import gitcheck
from .guard.protected import protected_among

# Parallel (pytest-xdist); the worker package is on the tests' path (backend/pyproject.toml).
DEFAULT_TEST = "cd backend && python -m pytest -q -n auto -p no:cacheprovider"
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
    branch: str = ""  # what was deployed: main (follow mode) or the promoted branch
    reason: str = ""  # one line: why it did not ship (the stage says where)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def identity(repo: Path) -> list[str]:
    """`-c user.name=… -c user.email=…` for the commits the deployer makes itself (the promote
    merge, a revert). Its container has no git identity, and git refuses to commit without one
    (deploys 7 and 8 were rejected at stage=merge for that). DEPLOY_GIT_NAME / DEPLOY_GIT_EMAIL
    always win; otherwise the checkout's own identity stays and only a missing one is filled."""
    args: list[str] = []
    for key, env, default in (("user.name", "DEPLOY_GIT_NAME", "PersonalOS Deployer"),
                              ("user.email", "DEPLOY_GIT_EMAIL", "deployer@personalos.local")):
        forced = os.environ.get(env, "").strip()
        if not forced:
            have = subprocess.run(["git", "config", "--get", key], cwd=repo, capture_output=True, text=True)
            if have.returncode == 0 and have.stdout.strip():
                continue
        args += ["-c", f"{key}={forced or default}"]
    return args


def is_deployer_revert(repo: Path, sha: str) -> bool:
    """A revert commit the deployer made itself (revert_to): never reverted again."""
    try:
        msg = git(repo, "log", "-1", "--format=%B", sha)
    except subprocess.CalledProcessError:
        return False
    return msg.startswith("Revert ") and bool(re.search(r"^Agent:\s*Deployer\s*$", msg, re.MULTILINE))


def sh(cmd: str, repo: Path, timeout: int = 1800, env: dict | None = None,
       tail: int | None = 6000) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=repo, shell=True, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    out = p.stdout + p.stderr
    return p.returncode == 0, out[-tail:] if tail else out


# The tests' own commits (fixtures that `git commit`) need an identity; the deployer's container
# has no global one, and a test that relied on it failed only there (deploy 52). The tests run
# with this one unless the environment brings its own.
TEST_GIT_IDENTITY = {"GIT_AUTHOR_NAME": "PersonalOS Tests", "GIT_AUTHOR_EMAIL": "tests@personalos.local",
                     "GIT_COMMITTER_NAME": "PersonalOS Tests", "GIT_COMMITTER_EMAIL": "tests@personalos.local"}


def failing_tests(out: str) -> list[str]:
    """The failing tests' names in pytest output (FAILED / ERROR lines, without the message)."""
    names: list[str] = []
    for ln in out.splitlines():
        m = re.match(r"^(?:FAILED|ERROR) (\S+)", ln.strip())
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def run_tests(cmd: str, repo: Path) -> tuple[bool, str]:
    """The test command, with a git identity, and a log that starts with the failing tests' names
    (all of them, not only what survives the tail of the output), so whoever fixes it can act."""
    env = {**os.environ}
    for k, v in TEST_GIT_IDENTITY.items():
        env.setdefault(k, v)
    ok, out = sh(cmd, repo, env=env, tail=None)
    if ok:
        return ok, out[-6000:]
    names = failing_tests(out)
    head = (f"Failing tests ({len(names)}):\n" + "\n".join(f"  - {n}" for n in names) if names else
            "Failing tests: pytest named none (a crash or a collection error?): see the end of the output.")
    return ok, f"{head}\n\n--- output (end) ---\n{out[-6000:]}"


# What the deployer's image is built from (ops/deployer.Dockerfile): a change here needs a new deployer.
SELF_PATHS = ("backend/src/", "backend/pyproject.toml", "ops/deployer.Dockerfile")


def deployer_changed(repo: Path, old: str, new: str) -> bool:
    try:
        files = git(repo, "diff", "--name-only", old, new).splitlines()
    except subprocess.CalledProcessError:
        return False
    return any(f.startswith(SELF_PATHS) for f in files)


def restart_self(cmd: str, repo: Path) -> tuple[bool, str]:
    """Recreate the deployer's own container. `docker compose up deployer` run in here would stop
    this container halfway (T-305: SIGKILL, then nobody to start it again), so it runs in a
    detached one-off container from the deployer's own image, with the docker socket and the
    checkout at the same path, and waits a few seconds for this tick to finish first."""
    me = os.environ.get("HOSTNAME", "")
    p = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", me], capture_output=True, text=True)
    image = p.stdout.strip()
    if p.returncode != 0 or not image:
        return False, f"cannot find the deployer's own image ({me}): {p.stderr.strip()}"
    p = subprocess.run(["docker", "run", "-d", "--rm", "--name", f"pos-deployer-restart-{int(time.time())}",
                        "-v", "/var/run/docker.sock:/var/run/docker.sock", "-v", f"{repo}:{repo}",
                        "-w", str(repo), "--entrypoint", "sh", image, "-c", f"sleep 5 && {cmd}"],
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-2000:]


def _stop(signum, frame):  # noqa: ARG001
    """As PID 1 in its container Python ignores SIGTERM unless it has a handler, so `docker stop`
    waited out the grace period and killed it (exit 137, T-305). Exit cleanly instead; a running
    subprocess.run kills its child on the way out and the next start resumes (.pos-promote-state)."""
    print("SIGTERM: stopping", flush=True)
    raise SystemExit(0)


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
    sha = git(repo, *identity(repo), "commit-tree", tree, "-p", bad, "-m", msg)
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
        if is_deployer_revert(repo, new):
            # Circuit breaker: the range ends in our own revert and still fails. Another revert on
            # top would fail the same way (a new revert commit and a new ticket every tick): stop.
            res.status, res.stage = "error", stage
            res.log = (f"{log}\n\n--- {new[:10]} is the deployer's own revert and it failed too; not reverting "
                       "again: a person (the SRE) has to restore the platform ---")
            return res
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
    tool_problems = tools.check_range(repo, old, new)
    if tool_problems:
        out = fail("tools", "shared tools failed the guard review: " + "; ".join(tool_problems))
        out.status = "rejected" if out.status != "error" else out.status
        return out
    if check.unsigned_protected:
        res.log = "warning (signing not set up yet): " + "; ".join(
            f"{p.sha[:10]} changes {', '.join(p.paths)}" for p in check.unsigned_protected)
    if test_cmd:
        ok, log = run_tests(test_cmd, repo)
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

    def frozen(self) -> bool:
        """The kill switch is on: the deployer ships nothing (it is an agent too)."""
        try:
            r = self.http.get("/api/worker/me", headers=self.auth)
            r.raise_for_status()
            return bool(r.json().get("frozen"))
        except httpx.HTTPError:
            return True  # cannot tell: better not to deploy

    def last_good(self) -> str | None:
        r = self.http.get("/api/deploys/last", headers=self.auth)
        r.raise_for_status()
        return r.json().get("sha")

    def review(self, sha: str, base: str, author: str, subject: str, commits: int) -> dict:
        """The QA review gate (pos.deploy_review): {"status": pending | approved | returned, "note"}."""
        r = self.http.post("/api/deploys/review", headers=self.auth, json={
            "sha": sha, "base": base, "author": author, "subject": subject, "commits": commits})
        r.raise_for_status()
        return r.json()

    def report(self, res: Result) -> dict:
        r = self.http.post("/api/deploys", headers=self.auth, json={
            "old_sha": res.old, "new_sha": res.new, "status": res.status, "stage": res.stage,
            "log": res.log if len(res.log) <= 8000 else f"{res.log[:2500]}\n[...]\n{res.log[-5400:]}", "author": res.author, "reverted_sha": res.reverted_sha,
            "commits": len(res.commits), "branch": res.branch, "reason": res.reason or reason_of(res),
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
    res.branch = branch
    reporter.report(res)
    return res


def reason_of(res: Result) -> str:
    """A one-line reason for a rejection, from its stage and log (the deploy-health summary groups them)."""
    if res.status in ("ok", "nothing"):
        return ""
    lines = [ln.strip() for ln in (res.log or "").splitlines() if ln.strip()]
    if res.stage == "tests":
        failed = [ln for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
        pick = failed[0] if failed else (lines[-1] if lines else "")
        rest = failing_tests("\n".join(failed))[1:]
        extra = f" (+{len(rest)} more: {', '.join(rest)})" if rest else ""
        return _clip(f"tests: {pick[:200]}{extra}")
    return f"{res.stage}: {(lines[0] if lines else res.status)[:200]}"


def _clip(line: str, n: int = 300) -> str:
    return line if len(line) <= n else line[:n - 4] + " ..."


def _load(state: Path) -> dict | None:
    """The last attempt: None without one, {} for the old format (a bare sha)."""
    if not state.exists():
        return None
    try:
        last = json.loads(state.read_text(encoding="utf-8").strip())
    except ValueError:
        return {}
    return last if isinstance(last, dict) else {}


REJECTED_KEPT = 50  # the (tip, base) pairs refused lately: none of them is tried again


def _remember(state: Path, tip: str, base: str, stage: str, conflict: list[str] | None = None) -> None:
    last = _load(state) or {}
    rejected = [p for p in last.get("rejected") or [] if isinstance(p, list) and len(p) == 2]
    if stage != "ok" and [tip, base] not in rejected:
        rejected = (rejected + [[tip, base]])[-REJECTED_KEPT:]
    data: dict = {"tip": tip, "base": base, "stage": stage, "rejected": rejected}
    if conflict is not None:
        data["conflict"] = sorted(conflict)
    state.write_text(json.dumps(data), encoding="utf-8")


def _should_try(state: Path, tip: str, base: str, clean=None, same_conflict=None) -> bool:
    """Not the same attempt again: a tip is tried once. A tip parked on a merge conflict is tried
    again only once it merges cleanly into the new main (`clean(tip, base)`, a silent
    `git merge-tree` check: no report, no task); the same (tip, base) pair never twice. The old
    state file held a bare sha and did not say why the tip failed (deploys 7 and 8: the missing
    git identity), so such a tip gets one more try. Deleting .pos-promote-state retries by hand."""
    last = _load(state)
    if not last:
        return True  # no attempt yet, or the old format: one more try with the fixed deployer
    if [tip, base] in (last.get("rejected") or []):
        return False  # refused before, unchanged: never the same attempt twice
    if last.get("tip") != tip:
        # A new commit on a parked branch that leaves the conflict as it was (deploys 46-51: six
        # tips, the same conflict in the same file, six refusals): stays parked, silently.
        return not (last.get("stage") == "merge" and same_conflict and same_conflict(last, tip, base))
    if last.get("stage") != "merge" or last.get("base") == base:
        return False
    return bool(clean and clean(tip, base))  # still conflicting (or cannot tell): stays parked


def publish_main(wt: Path, target: str, sha: str) -> str:
    """Move the checkout's own refs/heads/<target> to `sha`. The agents fetch main from this
    checkout's .git (mounted read-only as their remote `deployer`), but the promote push only
    moves <remote>/<target>: the local branch stood on ce70637 while production ran 11741bc,
    and every rebase onto it conflicted at the merge again (T-730). Fast-forward only.
    Returns "" or one line saying why the mirror is not on `sha`."""
    ref = f"refs/heads/{target}"
    have = subprocess.run(["git", "rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd=wt,
                          capture_output=True, text=True).stdout.strip()
    if have == sha:
        return ""
    if have and subprocess.run(["git", "merge-base", "--is-ancestor", have, sha], cwd=wt).returncode != 0:
        return f"mirror: {ref} {have[:10]} is not an ancestor of {sha[:10]}; not moved (a person has to look)"
    moved = subprocess.run(["git", "update-ref", ref, sha, *([have] if have else [])], cwd=wt,
                           capture_output=True, text=True)
    now = subprocess.run(["git", "rev-parse", "-q", "--verify", ref], cwd=wt, capture_output=True, text=True)
    if moved.returncode != 0 or now.stdout.strip() != sha:
        return (f"mirror: {ref} is {now.stdout.strip()[:10] or 'missing'}, not the merge {sha[:10]}: "
                f"{(moved.stderr or '').strip()[-300:]}")
    return ""


def mirror_check(wt: Path, remote: str, target: str, prod: str | None) -> tuple[bool, str]:
    """The deployer's health check: its <remote>/<target> = the mirror's <target> (what the
    agents fetch as deployer/<target>) and contains production (the last good deploy). The owner
    may push to main and deploy by hand, outside the deployer, so main can be ahead of the last
    deploy record; a mirror behind or beside production fails (T-784)."""
    def rev(name: str) -> str:
        return subprocess.run(["git", "rev-parse", "-q", "--verify", name], cwd=wt,
                              capture_output=True, text=True).stdout.strip()

    deployer, mirror = rev(f"{remote}/{target}" if remote else target), rev(f"refs/heads/{target}")
    line = f"deployer {deployer[:10] or '?'}, mirror {mirror[:10] or '?'}, prod {(prod or '?')[:10]}"
    if not deployer or deployer != mirror:
        return False, line
    if prod and prod != mirror:
        if not _is_ancestor(wt, prod, mirror):
            return False, line + " (prod is not in main)"
        line += " (main ahead of prod)"
    return True, line


def conflicting_files(wt: Path, tip: str, base: str) -> list[str] | None:
    """The files `tip` conflicts in when merged into `base` ([] = it merges cleanly, None = git
    could not tell). Checked without touching the worktree."""
    p = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", base, tip],
                       cwd=wt, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode == 0:
        return []
    if p.returncode != 1:
        return None
    return [f for f in p.stdout.splitlines()[1:] if f.strip()]


def unchanged_conflict(wt: Path, last: dict, tip: str, base: str) -> bool:
    """`tip` (new) still conflicts with `base`, and what it added since the parked tip touches none of
    the conflicting files: the same conflict, nothing to try again. A tip that rewrote the branch (a
    rebase), touches a conflicting file or merges cleanly now is worth an attempt."""
    old = last.get("tip") or ""
    now = conflicting_files(wt, tip, base)
    if not now or not old or not _is_ancestor(wt, old, tip):
        return False
    files = set(last.get("conflict") or []) | set(now)
    touched = set(git(wt, "diff", "--name-only", old, tip).splitlines())
    return not (touched & files)


def merges_cleanly(wt: Path, tip: str, base: str) -> bool:
    """Would `tip` merge into `base` without conflicts? Checked without touching the worktree."""
    p = subprocess.run(["git", "merge-tree", "--write-tree", base, tip], cwd=wt,
                       capture_output=True, text=True)
    return p.returncode == 0


def _conflict_detail(repo: Path) -> tuple[list[str], str]:
    """The conflicting files and their hunks (with the conflict markers) of a stopped merge or rebase."""
    files = [f for f in git(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if f]
    hunks = subprocess.run(["git", "diff", "--diff-filter=U"], cwd=repo, capture_output=True, text=True,
                           encoding="utf-8", errors="replace").stdout
    return files, hunks


def _is_ancestor(wt: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=wt,
                          capture_output=True).returncode == 0


def auto_rebase(wt: Path, base: str, tip: str) -> tuple[str | None, list[str], str]:
    """Rebase `tip` onto `base` once, in a scratch worktree (the deploy worktree and the agent's
    branch stay untouched). Returns (the rebased tip, [], "") or (None, conflicting files, hunks)."""
    scratch = Path(tempfile.mkdtemp(prefix="pos-rebase-"))
    subprocess.run(["git", "worktree", "prune"], cwd=wt, capture_output=True)
    try:
        git(wt, "worktree", "add", "--detach", "--force", str(scratch), tip)
        r = subprocess.run(["git", *identity(wt), "rebase", base], cwd=scratch, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            files, hunks = _conflict_detail(scratch)
            subprocess.run(["git", "rebase", "--abort"], cwd=scratch, capture_output=True)
            return None, files, hunks or (r.stdout + r.stderr)
        return git(scratch, "rev-parse", "HEAD"), [], ""
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(scratch)], cwd=wt, capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=wt, capture_output=True)


def _touches_protected(wt: Path, commits: list[str]) -> bool:
    return any(protected_among(gitcheck.changed_paths(wt, c)) for c in commits)


def conflict_log(source: str, target: str, files: list[str], hunks: str, why: str) -> str:
    listing = "\n".join(f"  - {f}" for f in files) or "  (git named no file)"
    return (f"CONFLICT: {source} does not merge into {target}. Conflicting files:\n{listing}\n\n"
            f"Not promoted: {why}. The deployer will not try this commit again.\n"
            f"What to do: rebase your branch onto the current {target} (git fetch deployer {target}, then "
            f"git rebase deployer/{target}: the deployer's checkout; origin can be days behind), resolve the conflicts in the files above, run the tests, and commit. "
            f"The new commit is picked up on the next tick.\n\nHunks:\n{hunks[-3500:]}")


def promote_tick(wt: Path, reporter: Reporter, *, source: str, remote: str, target: str, test_cmd: str,
                 up_cmd: str, health_url: str | None, source_remote: str | None = None,
                 require_review: bool = False) -> Result:
    """One promotion attempt of `source` into `remote/target`, in the worktree `wt`.
    `source_remote`: fetch it first when the branch lives in another repository
    (on the server: the Dev agent's clone, e.g. source "dev/agent/dev")."""
    state = wt / ".pos-promote-state"
    if source_remote:
        git(wt, "fetch", source_remote)
    git(wt, "fetch", remote, target)
    base = git(wt, "rev-parse", f"{remote}/{target}")
    tip = git(wt, "rev-parse", source)
    if err := publish_main(wt, target, base):  # heals a mirror left behind (main moved elsewhere)
        print(err, flush=True)
    if _is_ancestor(wt, tip, base):
        return Result(base, tip, "nothing")  # everything on the branch is already in main
    if not _should_try(state, tip, base, lambda t, b: merges_cleanly(wt, t, b),
                       lambda last, t, b: unchanged_conflict(wt, last, t, b)):
        last = _load(state) or {}
        parked = last.get("stage") == "merge"
        if parked and (last.get("base"), last.get("tip")) != (base, tip):  # checked against this main too
            _remember(state, tip, base, "merge", sorted(set(last.get("conflict") or [])
                                                        | set(conflicting_files(wt, tip, base) or [])))
        return Result(base, tip, "nothing", stage="parked" if parked else "")  # wait for a new commit
    commits = [c for c in git(wt, "rev-list", f"{base}..{tip}").splitlines() if c]
    res = Result(base, tip, "ok", author=author_of(wt, base, tip), commits=commits, branch=source)

    def fail(stage: str, log: str, conflict: list[str] | None = None) -> Result:
        res.status, res.stage, res.log = "rejected", stage, log
        _remember(state, tip, base, stage, conflict)
        reporter.report(res)
        return res

    git(wt, "checkout", "--detach", "--force", base)
    git(wt, "reset", "--hard", base)
    git(wt, "clean", "-fd", "-e", ".pos-promote-state")  # keeps ignored files (venv, node_modules)

    check = gitcheck.check_range(wt, base, tip)
    if check.problems:
        return fail("constitution", "; ".join(
            f"{p.sha[:10]} touches {', '.join(p.paths)} without the owner's signature" for p in check.problems))
    tool_problems = tools.check_range(wt, base, tip)
    if tool_problems:
        return fail("tools", "shared tools failed the guard review: " + "; ".join(tool_problems))
    subject = git(wt, "log", "-1", "--format=%s", tip)
    if require_review:  # DEPLOY_REQUIRE_REVIEW=1: the QA Reviewer approves the range first (pos.deploy_review)
        verdict = reporter.review(tip, base, res.author or "", subject, len(commits))
        if verdict.get("status") == "pending":
            return Result(base, tip, "nothing", stage="review", log=f"waiting for review {verdict.get('task') or ''}")
        if verdict.get("status") == "returned":
            return fail("review", f"returned by the QA review: {verdict.get('note') or ''}")
    def merge(rev: str, note: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *identity(wt), "merge", "--no-ff", "--no-edit", "-m",
             f"Merge {source}: {subject}\n\nPromoted by the PersonalOS deployer after checks{note}.\n\n"
             f"Agent: {res.author or 'unknown'}", rev], cwd=wt, capture_output=True, text=True)

    def conflict(files: list[str], hunks: str, why: str) -> Result:
        res.reason = _clip(f"merge conflict in {', '.join(files) or '?'}")
        return fail("merge", conflict_log(source, target, files, hunks, why), conflict=files)

    protected = _touches_protected(wt, commits)
    candidate, note = tip, ""
    if not _is_ancestor(wt, base, tip) and not protected:  # behind main: rebase first, not only on a conflict
        rebased, files, hunks = auto_rebase(wt, base, tip)
        if rebased:
            candidate, note = rebased, f" (rebased onto {target} automatically)"
            res.log = (f"{source} was behind {target}: promoted after an automatic rebase onto {target} "
                       f"({rebased[:10]}); {target} records {tip[:10]} as merged, so the branch fast-forwards "
                       f"(git fetch deployer {target} && git merge --ff-only deployer/{target}).")
        elif not merges_cleanly(wt, tip, base):  # a merge can still be clean (main took the branch's side)
            return conflict(files, hunks, f"an automatic rebase onto {target} {base[:10]} conflicted")
    first = merge(candidate, note)
    if first.returncode == 0 and candidate != tip:
        # The rebase's tree, but the agent's own tip as the second parent (not the rebased copies):
        # main then contains agent/dev, the next tick merges only what is new on it, and the agent
        # fast-forwards. Nothing pushes to the agent's clone (it is read-only here) or rewrites main.
        msg = git(wt, "log", "-1", "--format=%B", "HEAD")
        git(wt, "reset", "--hard", git(wt, *identity(wt), "commit-tree", "HEAD^{tree}", "-p", base, "-p", tip,
                                       "-m", msg))
    if first.returncode != 0:  # only a branch merged as it is (protected paths) can conflict here
        files, hunks = _conflict_detail(wt)
        subprocess.run(["git", "merge", "--abort"], cwd=wt, capture_output=True)
        git(wt, "reset", "--hard", base)
        why = ("no automatic rebase: the branch changes protected paths (a rebase drops the owner's signatures)"
               if protected else "the merge conflicted")
        return conflict(files, hunks or (first.stdout + first.stderr), why)
    if test_cmd:
        ok, log = run_tests(test_cmd, wt)
        if not ok:
            return fail("tests", log)

    def rolled_back(stage: str, log: str) -> Result:
        """The new build is (partly) live but broken: put main's last good
        version back up before reporting, so production never stays on it."""
        git(wt, "checkout", "--detach", "--force", base)
        ok, up_log = sh(up_cmd, wt)
        healthy_again = healthy(health_url)[0] if (ok and health_url) else ok
        if ok:
            note = f"\n\n--- rolled back to {base[:10]}{' (healthy)' if healthy_again else ' (still unhealthy)'} ---"
        else:
            note = f"\n\n--- rollback to {base[:10]} failed ---\n{up_log}"
        out = fail(stage, log + note)
        if not ok or not healthy_again:
            out.status = "error"
        return out

    if up_cmd:
        ok, log = sh(up_cmd, wt)
        if not ok:
            return rolled_back("deploy", log)
    if health_url:
        ok, log = healthy(health_url)
        if not ok:
            return rolled_back("health", log) if up_cmd else fail("health", log)
    merged = git(wt, "rev-parse", "HEAD")
    push = subprocess.run(["git", "push", remote, f"HEAD:{target}"], cwd=wt, capture_output=True, text=True)
    if push.returncode != 0:  # main moved meanwhile: try again next tick, not the author's fault
        return Result(base, tip, "nothing", stage="push", log=push.stderr[-2000:])
    _remember(state, tip, base, "ok")
    res.new = merged
    if err := publish_main(wt, target, merged):  # main is out: the error goes to the log, not a rejection
        print(err, flush=True)
        res.log = f"{res.log}\n{err}".strip()
    reporter.report(res)
    return res


# Not walked when giving files back: dependencies and the stacks' data (their owners are the containers').
OWNER_SKIP = {"node_modules", ".venv", "venv", "data", "dist", ".pytest_cache"}


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _owner(path: str) -> tuple[int, int]:
    st = os.lstat(path)
    return st.st_uid, st.st_gid


def _chown(path: str, uid: int, gid: int) -> None:
    os.lchown(path, uid, gid)


def restore_owner(repo: Path, tree: bool = True) -> int:
    """The deployer container runs as root (it drives docker), so its git fetch, checkout
    and merge leave root-owned objects and files in the owner's checkout, and the owner's
    own `git fetch` then fails. After every tick, give back to the checkout's owner every
    entry under the git directory (and, after a deploy, the working tree) that is not
    theirs. A no-op unless running as root on a checkout owned by someone else."""
    if not _is_root():
        return 0
    try:
        uid, gid = _owner(str(repo))
    except OSError:
        return 0
    if uid == 0:
        return 0
    try:
        common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    except (subprocess.CalledProcessError, OSError):
        common = repo / ".git"
    roots = [common] + ([repo] if tree else [])
    fixed = 0

    def give(path: str) -> None:
        nonlocal fixed
        try:
            if _owner(path)[0] != uid:
                _chown(path, uid, gid)
                fixed += 1
        except OSError:
            pass

    for top in roots:
        give(str(top))
        for root, dirs, files in os.walk(top):
            if top == repo:
                dirs[:] = [d for d in dirs if d not in OWNER_SKIP and Path(root, d) != common]
            for name in (*dirs, *files):
                give(os.path.join(root, name))
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", type=int, default=0, help="seconds between checks")
    ap.add_argument("--promote-from", default="", help="branch agents commit to (promote mode)")
    ap.add_argument("--source-remote", default="", help="remote to fetch the promote branch from first")
    ap.add_argument("--check-mirror", action="store_true",
                    help="health check: exit 1 unless <remote>/main = the checkout's main = the last good deploy")
    a = ap.parse_args()
    if os.environ.get("POS_CHILD_PIDFILE"):  # the real interpreter pid (a venv python.exe is only a launcher)
        open(os.environ["POS_CHILD_PIDFILE"], "w").write(str(os.getpid()))
    signal.signal(signal.SIGTERM, _stop)
    self_cmd = os.environ.get("DEPLOY_SELF_CMD", "")
    reporter = Reporter(os.environ.get("POS_URL", "http://localhost:8000"), os.environ["POS_DEPLOYER_KEY"])
    kw = dict(remote=os.environ.get("DEPLOY_REMOTE", "origin"), branch=os.environ.get("DEPLOY_BRANCH", "main"),
              test_cmd=os.environ.get("DEPLOY_TEST_CMD", DEFAULT_TEST), up_cmd=os.environ.get("DEPLOY_UP_CMD", DEFAULT_UP),
              health_url=os.environ.get("DEPLOY_HEALTH_URL", "http://localhost:8090/api/health"))
    if a.check_mirror:  # health check: deployer's main = the agents' deployer/main = production
        ok, line = mirror_check(Path(a.repo), kw["remote"], kw["branch"], reporter.last_good())
        print(f"mirror {'ok' if ok else 'MISMATCH'}: {line}", flush=True)
        raise SystemExit(0 if ok else 1)
    # A tick with nothing to promote prints nothing: this line shows the process is up (T-784).
    print(f"deployer started: {a.promote_from or kw['remote'] + '/' + kw['branch']} every {a.watch}s", flush=True)
    while True:
        if reporter.frozen():
            print("kill switch is on (or PersonalOS is unreachable): not deploying", flush=True)
            if a.once or not a.watch:
                break
            time.sleep(a.watch)
            continue
        try:
            if a.promote_from:
                res = promote_tick(Path(a.repo), reporter, source=a.promote_from, remote=kw["remote"],
                                   target=kw["branch"], test_cmd=kw["test_cmd"], up_cmd=kw["up_cmd"],
                                   health_url=kw["health_url"], source_remote=a.source_remote or None,
                                   require_review=os.environ.get("DEPLOY_REQUIRE_REVIEW") == "1")
            else:
                res = tick(Path(a.repo), reporter, **kw)
        except subprocess.CalledProcessError as e:
            # e.g. no access to the remote yet (a missing deploy key): say so and try again, never crash-loop
            restore_owner(Path(a.repo))
            err = (e.stderr or "").strip().splitlines()
            print(f"git {' '.join(e.cmd[1:3])} failed: {err[-1] if err else e}", flush=True)
            if a.once or not a.watch:
                raise
            time.sleep(max(a.watch, 300))
            continue
        fixed = restore_owner(Path(a.repo), tree=res.status != "nothing")
        if fixed:
            print(f"gave {fixed} root-owned files back to the checkout's owner", flush=True)
        if res.status != "nothing":
            print(f"{res.old[:10]}..{res.new[:10]}: {res.status} {res.stage}", flush=True)
        if res.status == "ok" and a.promote_from:
            ok, line = mirror_check(Path(a.repo), kw["remote"], kw["branch"], res.new)
            print(f"mirror {'ok' if ok else 'MISMATCH'}: {line}", flush=True)
        if res.status == "ok" and self_cmd and deployer_changed(Path(a.repo), res.old, res.new):
            ok, out = restart_self(self_cmd, Path(a.repo))
            print(f"recreating the deployer from a helper container: {'started' if ok else 'failed'} {out}", flush=True)
        if a.once or not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
