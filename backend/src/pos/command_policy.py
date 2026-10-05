"""Which shell commands an agent runs without asking, and who approves the rest (2026-10: 39 % of
the agents' Bash calls failed on approval: git fetch/rebase, ruff, pytest, npm ci/build blocked for
engineers, and every approval ended up with the owner).

Layered on the constitution's command guard (pos.guard.commands), never instead of it: the guard
decides first (bypass refused, irreversible and outside-triggered destructive commands to the owner,
U2-U4). Of what it allows:

- **auto-allowed** (`auto_allow`): ordinary engineering work inside the agent's own worktree (the
  worker's POS_AGENT_WORKDIR): local git (fetch, rebase, merge, revert, commit, status, log, diff, ...),
  read-only git (cat-file, ls-tree, rev-list, --version; also `git -C` into another checkout under
  /work), git worktree add/list inside the worktree, rm/cp/mv/mkdir/touch and `sed -i` when every
  path is inside the worktree (never the worktree itself or .git), ruff, pytest, py_compile, npm
  ci/install/test/run build, npx vite build / tsc, and read-only filters in a pipeline. The worker's command hook then answers "allow", so the CLI's own allow-list
  does not stop it. Anything that could run arbitrary code through git (`-c`, `--exec`, `rebase -x`,
  `--upload-pack`), command substitution, a path outside the worktree or a file redirect is not.
- **needs the CTO** (`needs_cto`): a push and other network writes outside the deploy flow (the
  deployer promotes agent/dev itself): one pending approval and one task for the CTO per command, not
  for the owner. The CTO decides with command_approval_decide; an approved command then runs for that
  agent (exactly that command, APPROVAL_DAYS days).
- everything else: as before, the CLI's allow-list in the agent's profile decides. A command it
  refuses can be put to the CTO with request_command_approval.
"""

import hashlib
import posixpath
import re
import shlex
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit
from .core import Ctx, Forbidden, NotFound, now_iso

APPROVAL_DAYS = 7

_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_UNSAFE = ("`", "$(", "<(", ">(", "\n", "\r")
_HARMLESS_REDIRECTS = {"2>&1", ">/dev/null", "2>/dev/null", "1>/dev/null", "&>/dev/null"}

SAFE_GIT = {"fetch", "rebase", "merge", "revert", "commit", "status", "log", "diff", "show", "add", "stash",
            "rev-parse", "switch", "checkout", "cherry-pick", "restore", "branch", "ls-files", "blame",
            "merge-base", "describe", "shortlog", "cat-file", "ls-tree", "rev-list", "worktree"}
# Read-only: also allowed with `git -C <path>` into another checkout under /work (a mounted repo).
GIT_READ_ONLY = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "merge-base", "describe",
                 "shortlog", "cat-file", "ls-tree", "rev-list"}
READ_ONLY_ROOTS = ("/work",)
WORKTREE_SAFE = {"list", "add", "prune"}
# File commands that only touch paths: allowed when every path is inside the worktree (never the root).
FILE_CMDS = {"rm", "cp", "mv", "mkdir", "touch"}
# git options that run programs or change where git works: never auto-allowed.
GIT_UNSAFE_OPTS = ("-c", "--exec", "-x", "--upload-pack", "--receive-pack", "--git-dir", "--work-tree",
                   "--exec-path", "-i", "--interactive", "--config-env")
BRANCH_WRITE = {"-d", "-D", "-m", "-M", "--delete", "--move", "-c", "-C", "--copy", "-f", "--force"}
NPM_SAFE = {"ci", "install", "i", "test", "t"}
NPM_RUN_SAFE = {"build", "test", "lint", "typecheck", "check"}
PY_MODULES = {"pytest", "ruff", "py_compile", "compileall", "mypy"}
FILTERS = {"head", "tail", "grep", "wc", "sort", "uniq", "cut", "tr", "true"}
READERS = {"ls", "pwd", "echo", "cat"}

_CTO_PATTERNS = [
    (r"\bgit\s+push\b", "pushes to a remote (the deployer promotes agent/dev; a push is outside that flow)"),
    (r"\b(curl|wget|http|https)\b[^\n;&|]*(\s-X\s*(POST|PUT|PATCH|DELETE)\b|\s--request\s+(POST|PUT|PATCH|DELETE)\b"
     r"|\s(-d|--data\S*|-F|--form|--upload-file|-T|--post-data|--post-file)\b)", "writes to a network service"),
    (r"\b(scp|rsync|sftp)\b[^\n;&|]*\S+:\S*", "copies to another host"),
    (r"(^|[;&|]\s*)ssh\s", "opens a shell on another host"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bdocker\s+push\b|\btwine\s+upload\b", "publishes a package or image"),
]


# ------------------------------------------------------------------ classification

def _inside(path: str, cwd: str, workdir: str) -> bool:
    full = posixpath.normpath(posixpath.join(cwd, path))
    root = posixpath.normpath(workdir)
    return full == root or full.startswith(root.rstrip("/") + "/")


def _sed_script_safe(script: str) -> bool:
    """No `e` (run a command), `w`/`W` (write a file) or `r`/`R` (read a file) commands or s///e|w flags."""
    if re.search(r"(^|[;{}\n]|\s)[0-9$,/!]*\s*[eEwWrR](\s|$|;)", script):
        return False
    return re.search(r"s(.).*?\1.*?\1[gpiImM0-9]*[ew]", script) is None


def _sed(args: list[str], cwd: str, workdir: str, first: bool) -> bool:
    """sed (in place or as a filter): only plain scripts, only files inside the worktree."""
    scripts, files, i, in_place = [], [], 0, False
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith(("-i", "--in-place")):
            in_place = True
        elif a in ("-f", "--file") or a.startswith("--file="):
            return False  # a script file: not read here
        elif a.startswith("-") and a != "-":
            if not re.fullmatch(r"-[nErsuz]+|--(quiet|silent|regexp-extended|separate|null-data|posix|debug)", a):
                return False
        elif not scripts:
            scripts.append(a)
        else:
            files.append(a)
        i += 1
    if not scripts or not all(_sed_script_safe(x) for x in scripts):
        return False
    if not files:
        return not first and not in_place
    return all(_inside(f, cwd, workdir) for f in files)


def _files_cmd(cmd: str, args: list[str], cwd: str, workdir: str) -> bool:
    """rm/cp/mv/mkdir/touch with every path inside the worktree, and never the worktree itself or .git."""
    paths, opts_done = [], False
    for a in args:
        if a == "--" and not opts_done:
            opts_done = True
        elif a.startswith("-") and not opts_done:
            if a.startswith("--target-directory="):
                paths.append(a.split("=", 1)[1])
        else:
            paths.append(a)
    if not paths:
        return False
    root = posixpath.normpath(workdir)
    for p in paths:
        if not _inside(p, cwd, workdir):
            return False
        full = posixpath.normpath(posixpath.join(cwd, p))
        if cmd in ("rm", "mv") and (full == root or posixpath.basename(full) == ".git"):
            return False
    return True


def _python(tok: str) -> bool:
    base = posixpath.basename(tok)
    return base in ("python", "python3") or re.fullmatch(r"python3\.\d+", base) is not None


def _segment(words: list[str], cwd: str, workdir: str, first: bool) -> tuple[bool, str]:
    """(allowed, the cwd after it) for one simple command."""
    if not words:
        return True, cwd
    cmd, args = words[0], words[1:]
    paths = [a for a in args if not a.startswith("-")]
    if cmd == "cd":
        target = args[0] if args else workdir
        return _inside(target, cwd, workdir), posixpath.normpath(posixpath.join(cwd, target))
    if cmd in FILTERS:
        return (not first) or all(_inside(p, cwd, workdir) for p in paths[1 if cmd == "grep" else 0:]), cwd
    if cmd in READERS:
        return cmd in ("pwd", "echo") or all(_inside(p, cwd, workdir) for p in paths), cwd
    if cmd == "git":
        i = 0
        here = cwd
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C" and i + 1 < len(args):
                here = posixpath.normpath(posixpath.join(here, args[i + 1]))
                i += 2
                continue
            if args[i] in ("--no-pager", "-P"):
                i += 1
                continue
            if args[i] in ("--version", "--help") and i == len(args) - 1:
                return True, cwd
            return False, cwd
        if i >= len(args) or args[i] not in SAFE_GIT:
            return False, cwd
        sub, rest = args[i], args[i + 1:]
        if any(a == o or a.startswith(o + "=") for a in rest for o in GIT_UNSAFE_OPTS):
            return False, cwd
        if sub == "branch" and any(a in BRANCH_WRITE for a in rest):
            return False, cwd
        if sub == "worktree":
            if not rest or rest[0] not in WORKTREE_SAFE:
                return False, cwd
            if rest[0] == "add":  # the new worktree goes inside the agent's own folder too
                opts = rest[1:]
                values = {opts[k + 1] for k, a in enumerate(opts[:-1]) if a in ("-b", "-B", "--reason")}
                where = [a for a in opts if not a.startswith("-") and a not in values]
                if not where or not _inside(where[0], here, workdir):
                    return False, cwd
        if _inside(here, here, workdir):
            return True, cwd
        # A read-only look into another checkout under /work (a mounted repo).
        return sub in GIT_READ_ONLY and any(_inside(here, here, r) for r in READ_ONLY_ROOTS), cwd
    if cmd in FILE_CMDS:
        return _files_cmd(cmd, args, cwd, workdir), cwd
    if cmd == "sed":
        return _sed(args, cwd, workdir, first), cwd
    if cmd == "ruff":
        return bool(args) and args[0] in ("check", "format") and all(_inside(p, cwd, workdir) for p in paths[1:]), cwd
    if cmd in ("pytest", "py.test") or cmd.endswith(("/pytest", "/ruff")):
        return True, cwd
    if _python(cmd):
        return len(args) >= 2 and args[0] == "-m" and args[1] in PY_MODULES, cwd
    if cmd == "npm":
        rest = list(args)
        if rest[:1] == ["--prefix"] and len(rest) >= 2:
            if not _inside(rest[1], cwd, workdir):
                return False, cwd
            rest = rest[2:]
        if not rest:
            return False, cwd
        if rest[0] in NPM_SAFE:
            return True, cwd
        return rest[0] == "run" and len(rest) >= 2 and rest[1] in NPM_RUN_SAFE, cwd
    if cmd == "npx":
        rest = [a for a in args if a not in ("--no-install",)]
        return rest[:2] == ["vite", "build"] or rest[:1] == ["tsc"], cwd
    return False, cwd


def auto_allow(command: str, cwd: str | None, workdir: str | None) -> bool:
    """Is this ordinary engineering work inside the agent's own worktree (see the module doc)?"""
    if not workdir or not command.strip() or any(u in command for u in _UNSAFE):
        return False
    here = posixpath.normpath(cwd or workdir)
    if not _inside(here, here, workdir):
        return False
    for i, part in enumerate(p for p in _SEPARATORS.split(command.strip()) if p.strip()):
        try:
            words = shlex.split(part)
        except ValueError:
            return False
        words = [w for w in words if w not in _HARMLESS_REDIRECTS]
        if any(w.startswith((">", "<", "&>")) or re.match(r"^\d?>", w) for w in words):
            return False
        while words and re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S*", words[0]):  # FOO=1 cmd: only harmless names
            if words[0].split("=", 1)[0] in ("PATH", "LD_PRELOAD", "PYTHONPATH", "GIT_SSH_COMMAND", "NODE_OPTIONS"):
                return False
            words = words[1:]
        ok, here = _segment(words, here, workdir, first=(i == 0))
        if not ok:
            return False
    return True


def needs_cto(command: str) -> str | None:
    """Why this command needs the CTO's approval (a push or network write), or None."""
    for pattern, reason in _CTO_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE | re.MULTILINE):
            return reason
    return None


# ------------------------------------------------------------------ approvals

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS command_approvals (
        id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, command TEXT NOT NULL,
        reason TEXT, why TEXT, status TEXT NOT NULL DEFAULT 'pending', task_id INTEGER, approver_id INTEGER,
        decided_by INTEGER, decision TEXT, run_id INTEGER, created_at TEXT NOT NULL, decided_at TEXT,
        expires_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS command_approvals_fp ON command_approvals (actor_id, fingerprint, status)")


def fingerprint(command: str) -> str:
    return hashlib.sha256(" ".join(command.split()).encode("utf-8")).hexdigest()[:32]


def approver(conn: sqlite3.Connection, actor_id: int) -> sqlite3.Row:
    """The CTO; the owner only for the CTO's own commands or when there is no CTO."""
    cto = conn.execute("""SELECT * FROM actors WHERE role = 'cto' AND archived_at IS NULL AND id != ?
                          ORDER BY id LIMIT 1""", (actor_id,)).fetchone()
    return cto if cto is not None else actors.get(conn, actors.owner_id(conn))


def approved(conn: sqlite3.Connection, actor_id: int, command: str) -> bool:
    ensure_schema(conn)
    return conn.execute("""SELECT 1 FROM command_approvals WHERE actor_id = ? AND fingerprint = ?
                           AND status = 'approved' AND (expires_at IS NULL OR expires_at > ?)""",
                        (actor_id, fingerprint(command), now_iso())).fetchone() is not None


def request(conn: sqlite3.Connection, ctx: Ctx, command: str, why: str = "", reason: str = "") -> dict:
    """One pending approval (and one task for the approver) per agent and command."""
    ensure_schema(conn)
    command = command.strip()
    if not command:
        raise ValueError("an empty command")
    fp = fingerprint(command)
    row = conn.execute("""SELECT * FROM command_approvals WHERE actor_id = ? AND fingerprint = ? AND status = 'pending'
                          ORDER BY id DESC LIMIT 1""", (ctx.actor_id, fp)).fetchone()
    if row is not None:
        return {**_out(conn, row), "deduped": True}
    who = approver(conn, ctx.actor_id)
    me = actors.get(conn, ctx.actor_id)
    cur = conn.execute("""INSERT INTO command_approvals (actor_id, fingerprint, command, reason, why, approver_id,
                          run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (ctx.actor_id, fp, command[:4000], reason[:300] or None, why[:1000] or None, who["id"],
                        ctx.run_id, now_iso()))
    aid = cur.lastrowid
    from . import tasks

    system = Ctx(actors.owner_id(conn), via="system")
    t = tasks.create(conn, system, {
        "title": f"Approve a command for {me['name']}: {command[:70]}",
        "notes": (f"Purpose: {me['name']} needs a shell command its guard does not run on its own. "
                  f"Source: the command policy (pos.command_policy), approval #{aid}.\n\n"
                  f"**Command**\n\n```\n{command[:2000]}\n```\n\n**Why it needs approval:** "
                  f"{reason or 'not on the agent allow-list'}\n\n**The agent's reason:** {why or '(none given)'}\n\n"
                  f"Decide with command_approval_decide(approval={aid}, approve=true|false, reason=...). "
                  f"Approved, exactly this command runs for {me['name']} for {APPROVAL_DAYS} days."),
        "definition_of_done": "The approval is decided (approved or refused with a reason); the agent hears it.",
        "priority": 2, "status": "next", "assignee": {"type": who["kind"], "id": who["id"]},
    })
    conn.execute("UPDATE command_approvals SET task_id = ? WHERE id = ?", (t["id"], aid))
    audit.log(conn, ctx, "command_approval_request", "task", t["id"], approval=aid, approver=who["name"],
              command=command[:300])
    return {**_out(conn, conn.execute("SELECT * FROM command_approvals WHERE id = ?", (aid,)).fetchone()),
            "task": t["ref"]}


def may_decide(conn: sqlite3.Connection, actor_id: int, row) -> bool:
    me = actors.get(conn, actor_id)
    return bool(me["is_owner"]) or actor_id == row["approver_id"] or (me["role"] or "") == "cto"


def decide(conn: sqlite3.Connection, ctx: Ctx, approval_id: int, approve: bool, reason: str = "") -> dict:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM command_approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise NotFound(f"no command approval #{approval_id}")
    if row["actor_id"] == ctx.actor_id:
        raise Forbidden("an agent does not approve its own command")
    if not may_decide(conn, ctx.actor_id, row):
        raise Forbidden("the CTO (or the owner) decides command approvals")
    if row["status"] != "pending":
        raise Forbidden(f"approval #{approval_id} is {row['status']} already")
    expires = (datetime.now(timezone.utc) + timedelta(days=APPROVAL_DAYS)).isoformat(timespec="seconds")
    conn.execute("""UPDATE command_approvals SET status = ?, decided_by = ?, decision = ?, decided_at = ?,
                    expires_at = ? WHERE id = ?""",
                 ("approved" if approve else "refused", ctx.actor_id, reason[:1000] or None, now_iso(),
                  expires if approve else None, approval_id))
    audit.log(conn, ctx, "command_approval_decide", "actor", row["actor_id"], approval=approval_id,
              approve=approve, reason=reason[:300] or None)
    from . import chat

    try:
        verdict = "approved: run it again now" if approve else f"refused: {reason or 'no reason given'}"
        chat.send_dm(conn, Ctx(ctx.actor_id, via="system"), row["actor_id"],
                     f"Command approval #{approval_id} {verdict}.\n\n`{row['command'][:300]}`")
    except Exception:  # noqa: BLE001 - the decision stands; the agent sees it in its next check
        pass
    if row["task_id"]:
        from . import tasks

        try:
            tasks.complete(conn, ctx, row["task_id"], f"{'Approved' if approve else 'Refused'}: {reason}".strip())
        except Exception:  # noqa: BLE001 - a task someone moved on already
            pass
    return _out(conn, conn.execute("SELECT * FROM command_approvals WHERE id = ?", (approval_id,)).fetchone())


def _out(conn: sqlite3.Connection, row) -> dict:
    from . import tasks

    return {"approval": row["id"], "status": row["status"], "command": row["command"],
            "task": tasks.display_id(row["task_id"]) if row["task_id"] else None,
            "approver": actors.get(conn, row["approver_id"])["name"] if row["approver_id"] else None,
            "expires_at": row["expires_at"]}


# ------------------------------------------------------------------ the guard endpoint

def check(conn: sqlite3.Connection, ctx: Ctx, command: str, cwd: str | None, workdir: str | None) -> dict:
    """What /api/worker/check-command adds once the constitution's guard allowed a command:
    {"outcome": "allow", "auto": bool} or {"outcome": "needs_cto", ...}."""
    if approved(conn, ctx.actor_id, command):
        return {"outcome": "allow", "auto": True, "reason": "approved by the CTO"}
    why = needs_cto(command)
    if why:
        got = request(conn, ctx, command, reason=why)
        return {"outcome": "needs_cto", "rule": "cto", "reason": f"Needs the CTO's approval: {why}.",
                "approval": got["approval"], "cto_task": got.get("task")}
    if auto_allow(command, cwd, workdir):
        return {"outcome": "allow", "auto": True, "reason": "engineering work inside your worktree"}
    return {"outcome": "allow", "auto": False}


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    from . import mcp_server

    mcp_server.TOOL_PERMISSIONS.setdefault("request_command_approval", "tasks:claim")
    mcp_server.TOOL_PERMISSIONS.setdefault("command_approval_decide", "tasks:claim")

    @mcp.tool(description="A shell command your guard refused ('requires approval'): ask the CTO to approve "
                          "exactly this command for you (one task for the CTO, never the owner). why: one "
                          "sentence on what it is for. Approved, it runs for you for 7 days; the verdict reaches "
                          "your inbox. Local git, ruff, pytest and npm build/test in your worktree need no approval.")
    def request_command_approval(ctx: Context, command: str, why: str) -> dict:
        with session(ctx, "request_command_approval") as (conn, c):
            try:
                return request(conn, c, command, why=why, reason="refused by the agent's allow-list")
            except ValueError as e:
                raise ToolError(str(e)) from e

    @mcp.tool(description="The CTO decides a command approval (approval id from its task): approve true runs "
                          "exactly that command for that agent for 7 days; false refuses it (say why in reason).")
    def command_approval_decide(ctx: Context, approval: int, approve: bool, reason: str = "") -> dict:
        with session(ctx, "command_approval_decide", approval=approval, approve=approve) as (conn, c):
            return decide(conn, c, approval, approve, reason)


def summary(conn: sqlite3.Connection) -> dict:
    ensure_schema(conn)
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM command_approvals GROUP BY status")}

