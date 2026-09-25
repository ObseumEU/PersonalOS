"""Git enforcement: protected paths change only in commits the owner signed.

Used in three places:
- the agent runner, before it pushes an agent's work to main;
- a ``pre-receive`` hook on the server's repo (ops/git-hooks/pre-receive);
- CI on GitHub (.github/workflows/constitution.yml), which cannot block a
  push but fails loudly so the change gets reverted.

The owner signs commits with an SSH key. The keys allowed to sign are in
``ops/owner_allowed_signers`` (git's allowed-signers format), itself a
protected file. Signatures are always verified against that file as it was
*before* the pushed commits, so a push cannot add a key and then use it.
``POS_OWNER_SIGNERS`` may point to a file outside the repo instead (e.g.
/etc/personalos/allowed_signers owned by root on the server).

Agents do not hold the owner's key, so they cannot forge a signature even
though they can set any author name.

Until the owner adds ``ops/owner_allowed_signers`` the constitution is a
draft and the check only warns.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .protected import protected_among

ZERO_SHA = "0" * 40
SIGNERS_PATH = "ops/owner_allowed_signers"


@dataclass(frozen=True)
class CommitProblem:
    sha: str
    paths: list[str]
    signature: str  # git's %G? code

    def describe(self) -> str:
        return (
            f"{self.sha[:12]} changes protected paths {', '.join(self.paths)} "
            f"without the owner's signature (signature status {self.signature!r})"
        )


def _git(repo: Path, *args: str, signers: Path | None = None) -> str:
    cmd = ["git", "-C", str(repo)]
    if signers:
        # Pin the verifier on the command line so repo config (which an agent
        # can edit) cannot swap in a program that always says "good".
        cmd += [
            "-c", f"gpg.ssh.allowedSignersFile={signers}",
            "-c", "gpg.ssh.program=ssh-keygen",
            "-c", "gpg.ssh.revocationFile=",
        ]
    return subprocess.run(
        [*cmd, *args], check=True, capture_output=True, text=True
    ).stdout


def commits_in_range(repo: Path, old: str, new: str) -> list[str]:
    if new == ZERO_SHA:
        return []  # branch deletion
    spec = [new, "--not", "--all"] if old == ZERO_SHA else [f"{old}..{new}"]
    out = _git(repo, "rev-list", *spec)
    return out.split()


def changed_paths(repo: Path, sha: str) -> list[str]:
    # --cc: for a merge, only files that differ from every parent, i.e. the
    # changes the merge itself introduces. Signed commits merged in are
    # checked on their own.
    out = _git(repo, "diff-tree", "-r", "--root", "--no-commit-id", "--name-only", "--cc", sha)
    return [line for line in out.splitlines() if line]


def owner_signed(repo: Path, sha: str, signers: Path | None, principals: set[str]) -> tuple[bool, str]:
    if signers is None or not signers.exists():
        return False, "N"
    out = _git(repo, "log", "-1", "--format=%G?%x00%GS", sha, signers=signers)
    status, _, signer = out.strip().partition("\x00")
    if status != "G":
        return False, status or "N"
    if principals and signer not in principals:
        return False, f"G:{signer}"
    return True, status


def signers_at(repo: Path, rev: str) -> str | None:
    """The allowed-signers file as it is at ``rev``, or None if it is missing."""
    try:
        return _git(repo, "show", f"{rev}:{SIGNERS_PATH}")
    except subprocess.CalledProcessError:
        return None


@dataclass(frozen=True)
class RangeResult:
    problems: list[CommitProblem]
    active: bool  # False while the owner has not set up signing keys yet
    unsigned_protected: list[CommitProblem]  # what would be rejected once active


def check_range(
    repo: Path,
    old: str,
    new: str,
    *,
    signers: Path | None = None,
    principals: set[str] | None = None,
    base: str | None = None,
) -> RangeResult:
    """Check every commit in old..new.

    ``base`` is the revision whose signers file is trusted; defaults to
    ``old``, or to ``main`` when a new branch is pushed.
    """
    signers = signers or _env_path("POS_OWNER_SIGNERS")
    principals = principals if principals is not None else _env_set("POS_OWNER_PRINCIPALS")
    tmp = None
    if signers is None:
        text = signers_at(repo, base or (old if old != ZERO_SHA else "main"))
        if text and text.strip():
            tmp = tempfile.NamedTemporaryFile("w", suffix=".signers", delete=False)
            tmp.write(text)
            tmp.close()
            signers = Path(tmp.name)
    active = signers is not None and signers.exists()
    try:
        found = []
        for sha in commits_in_range(repo, old, new):
            touched = protected_among(changed_paths(repo, sha))
            if not touched:
                continue
            ok, status = owner_signed(repo, sha, signers if active else None, principals)
            if not ok:
                found.append(CommitProblem(sha, touched, status))
    finally:
        if tmp:
            os.unlink(tmp.name)
    return RangeResult(found if active else [], active, found)


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _env_set(name: str) -> set[str]:
    return {p.strip() for p in os.environ.get(name, "").split(",") if p.strip()}
