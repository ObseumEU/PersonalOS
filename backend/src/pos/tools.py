"""The tool library: code and instructions agents write for themselves and
share with the team (docs/TOOLS.md).

Tools live in git next to the agents' instructions:
- ``agents/<agent-slug>/tools/<name>/``: personal tools, changed by commits;
- ``shared/tools/<name>/``: team tools, published through the deployer.

Each has a ``tool.json`` manifest. Kinds: ``script`` (a program the agent
runs), ``mcp`` (a small stdio MCP server the worker mounts), ``skill``
(SKILL.md instructions). The guard review (``review``) looks for secrets,
outbound calls the manifest does not declare, and permission escalation; the
deployer runs it on every commit that touches ``shared/tools/``
(``check_range``) and refuses the range on findings.

The repository root comes from POS_TOOLS_REPO_DIR, else the parent of
POS_AGENTS_REPO_DIR, else this checkout.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import actors, audit
from .core import Ctx, Forbidden, NotFound, now_iso

KINDS = ("script", "mcp", "skill")
VISIBILITIES = ("personal", "team")
SHARED_DIR = "shared/tools"
MANIFEST = "tool.json"
MAX_FILE_BYTES = 200_000
MAX_FILES = 60
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
_CODE_SUFFIXES = {".py", ".sh", ".bash", ".ps1", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ""}


class ToolError(ValueError):
    """A tool request that cannot be done (bad name, not yours, ...)."""


# ------------------------------------------------------------------ where tools live

def repo_root() -> Path | None:
    if os.environ.get("POS_TOOLS_REPO_DIR"):
        return Path(os.environ["POS_TOOLS_REPO_DIR"])
    if os.environ.get("POS_AGENTS_REPO_DIR"):
        return Path(os.environ["POS_AGENTS_REPO_DIR"]).parent
    here = Path(__file__).resolve().parents[3]  # backend/src/pos/tools.py -> repo root
    return here if (here / "agents").is_dir() or (here / "shared").is_dir() else None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"


@dataclass
class Tool:
    name: str
    scope: str  # "shared" or the owning agent's slug
    path: str  # the tool's folder, relative to the repo root
    manifest: dict
    files: dict[str, str] = field(default_factory=dict)  # path inside the folder -> text
    errors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.scope}/{self.name}"

    @property
    def shared(self) -> bool:
        return self.scope == "shared"

    @property
    def entry(self) -> str:
        return str(self.manifest.get("entry") or "")

    def summary(self) -> dict:
        m = self.manifest
        return {
            "id": self.id, "name": self.name, "scope": self.scope, "path": self.path,
            "kind": m.get("kind"), "description": m.get("description", ""), "owner": m.get("owner", ""),
            "visibility": m.get("visibility"), "version": m.get("version"), "entry": self.entry,
            "tests": m.get("tests"), "permissions_needed": list(m.get("permissions_needed") or []),
            "outbound": bool(m.get("outbound")), "outbound_kind": m.get("outbound_kind"), "valid": not self.errors, "errors": self.errors,
        }


def _parse_manifest(text: str | None) -> tuple[dict, list[str]]:
    if text is None:
        return {}, [f"missing {MANIFEST}"]
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, [f"{MANIFEST} is not valid JSON: {e}"]
    if not isinstance(data, dict):
        return {}, [f"{MANIFEST} must be an object"]
    return data, []


def validate(manifest: dict, files: dict[str, str], *, folder_name: str, shared: bool) -> list[str]:
    """Problems with a manifest, in plain words (empty when it is fine)."""
    from .agents import PERMISSIONS

    errs = []
    m = manifest
    if m.get("name") != folder_name:
        errs.append(f"name must match the folder ({folder_name})")
    if not _NAME_RE.match(str(m.get("name") or "")):
        errs.append("name: lower case letters, digits and dashes")
    if m.get("kind") not in KINDS:
        errs.append(f"kind must be one of {', '.join(KINDS)}")
    for key in ("description", "owner"):
        if not isinstance(m.get(key), str) or not m[key].strip():
            errs.append(f"{key} is required")
    if m.get("visibility") not in VISIBILITIES:
        errs.append("visibility must be personal or team")
    elif shared and m["visibility"] != "team":
        errs.append("a shared tool has visibility team")
    elif not shared and m["visibility"] != "personal":
        errs.append("a tool in an agent's folder has visibility personal (publish it to share it)")
    if not _SEMVER_RE.match(str(m.get("version") or "")):
        errs.append("version must be semver (1.2.3)")
    entry = str(m.get("entry") or "")
    if not entry or entry.startswith(("/", "\\")) or ".." in Path(entry).parts:
        errs.append("entry must be a file inside the tool folder")
    elif entry not in files:
        errs.append(f"entry {entry} does not exist")
    elif m.get("kind") == "skill" and Path(entry).name != "SKILL.md":
        errs.append("a skill's entry is SKILL.md")
    if not isinstance(m.get("tests"), str) or not m["tests"].strip():
        errs.append("tests is required (a command or a test file)")
    perms = m.get("permissions_needed", [])
    if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
        errs.append("permissions_needed must be a list of permissions")
    else:
        unknown = sorted(set(perms) - set(PERMISSIONS))
        if unknown:
            errs.append(f"unknown permissions: {', '.join(unknown)}")
    if not isinstance(m.get("outbound", False), bool):
        errs.append("outbound must be true or false")
    from .guard.policy import OUTBOUND_KINDS

    if m.get("outbound_kind") is not None and m.get("outbound_kind") not in OUTBOUND_KINDS:
        errs.append(f"outbound_kind must be one of {', '.join(OUTBOUND_KINDS)}")
    return errs


def _read_folder(folder: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or len(files) >= MAX_FILES:
            continue
        data = p.read_bytes()[:MAX_FILE_BYTES]
        if b"\0" in data:
            continue  # binary: not reviewed, and not something a tool should ship
        files[p.relative_to(folder).as_posix()] = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return files


def make_tool(name: str, scope: str, path: str, files: dict[str, str]) -> Tool:
    manifest, errs = _parse_manifest(files.get(MANIFEST))
    if manifest:
        errs += validate(manifest, files, folder_name=name, shared=scope == "shared")
        if scope != "shared" and slug(str(manifest.get("owner") or "")) != scope:
            errs.append(f"owner must be the agent whose folder it is ({scope})")
    return Tool(name, scope, path, manifest, files, errs)


def load(root: Path, rel: str) -> Tool:
    folder = root / rel
    parts = rel.split("/")
    scope = "shared" if rel.startswith(SHARED_DIR + "/") else parts[1]
    return make_tool(parts[-1], scope, rel, _read_folder(folder))


def discover(root: Path | None = None) -> list[Tool]:
    """Every tool in the repo: shared first, then each agent's personal ones."""
    root = root or repo_root()
    if root is None:
        return []
    found = []
    shared = root / SHARED_DIR
    if shared.is_dir():
        found += [load(root, f"{SHARED_DIR}/{d.name}") for d in sorted(shared.iterdir()) if d.is_dir()]
    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for a in sorted(agents_dir.iterdir()):
            if (a / "tools").is_dir():
                found += [load(root, f"agents/{a.name}/tools/{d.name}") for d in sorted((a / "tools").iterdir())
                          if d.is_dir() and not d.name.startswith((".", "_"))]
    return [t for t in found if not t.name.startswith((".", "_"))]


def find(name: str, agent_slug: str | None = None, root: Path | None = None) -> Tool:
    """The agent's personal tool of that name, else the shared one."""
    tools = discover(root)
    for t in tools:
        if t.name == name and t.scope == agent_slug:
            return t
    for t in tools:
        if t.name == name and t.shared:
            return t
    raise NotFound(f"tool {name}")


# ------------------------------------------------------------------ guard review

_SECRETS = [
    (re.compile(r"-----BEGIN ([A-Z]+ )?PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"), "a GitHub token"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}"), "an API key (sk-...)"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "a Google API key"),
    (re.compile(r"\bpos_[A-Za-z0-9_-]{30,}"), "a PersonalOS key"),
    (re.compile(r"\b[MN][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}\b"), "a Discord bot token"),
    (re.compile(r"""(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)\b"""
                r"""["']?\s*[:=]\s*["']([^"'\s]{12,})["']"""), "a hard-coded credential"),
]
_PLACEHOLDER = re.compile(r"(?i)example|placeholder|your[_-]|xxxx|changeme|dummy|<[^>]*>|\$\{|\{\{")

_OUTBOUND_CODE = [
    (re.compile(r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib3|smtplib|ftplib|paramiko|socket|"
                r"http\.client|urllib\.request|websockets?|discord|slack_sdk|telegram)\b", re.M),
     "imports a network or mail client"),
    (re.compile(r"\burlopen\s*\(|\burllib\.request\b"), "opens a URL"),
    (re.compile(r"\bfetch\s*\(|\baxios\b|\bXMLHttpRequest\b|\bWebSocket\s*\("), "makes an HTTP request"),
    (re.compile(r"""\brequire\(\s*['"](https?|net|node-fetch|nodemailer|axios)['"]\s*\)"""
                r"""|\bfrom\s+['"](node-fetch|nodemailer|axios|undici)['"]"""), "loads a network client"),
    (re.compile(r"(^|[\s;&|(`$])(curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|nc|ncat|scp|sftp|ftp)\s",
                re.M | re.I), "runs a network command"),
]
_OUTBOUND_ANY = [
    (re.compile(r"discord(app)?\.com/api/webhooks", re.I), "a Discord webhook URL"),
    (re.compile(r"hooks\.slack\.com", re.I), "a Slack webhook URL"),
    (re.compile(r"\bsmtps?://|\bsmtp\.[a-z0-9-]+\.[a-z]{2,}", re.I), "an SMTP server"),
    (re.compile(r"api\.telegram\.org/bot", re.I), "a Telegram bot URL"),
    (re.compile(r"outlook\.office\.com/webhook|webhook\.office\.com", re.I), "a Teams webhook URL"),
]
_ESCALATION = [
    (re.compile(r"\b(POS_DEPLOYER_KEY|DEPLOYER_KEY|POS_MCP_TOKEN|POS_SESSION_SECRET|POS_PASSWORD|"
                r"CLAUDE_CODE_OAUTH_TOKEN|POS_OWNER_SIGNERS)\b"), "reads a credential that is not the agent's own"),
    (re.compile(r"owner_allowed_signers"), "touches the owner's signing keys"),
    (re.compile(r"(^|[\s;&|(])sudo\s", re.M), "runs as root"),
    (re.compile(r"\bis_owner\s*=\s*1|\bUPDATE\s+actors\s+SET\s+permissions", re.I), "changes permissions directly"),
]


def _finding(kind: str, file: str, line: int | None, detail: str) -> dict:
    return {"kind": kind, "file": file, "line": line, "detail": detail}


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def owner_permissions(conn: sqlite3.Connection | None, owner: str) -> set[str]:
    """What the tool's owner may do. Without the database (the deployer), a
    conservative default: the owner is treated as an ordinary agent."""
    from .agents import BUILTIN_PERMISSIONS, DEFAULT_AGENT_PERMISSIONS, permissions_of

    if conn is not None:
        row = actors.find_by_name(conn, owner)
        if row is None:
            row = next((r for r in conn.execute("SELECT * FROM actors") if slug(r["name"]) == slug(owner)), None)
        if row is not None and not row["archived_at"]:
            return {"*"} if row["kind"] == "human" else permissions_of(conn, row["id"])
    return set(BUILTIN_PERMISSIONS.get(owner, DEFAULT_AGENT_PERMISSIONS))


def review(tool: Tool, owner_perms: set[str] | None = None) -> list[dict]:
    """The guard review: invalid manifest, secrets, undeclared outbound calls,
    bypasses and permission escalation. Returns findings (empty = clean)."""
    from .guard import commands

    out = [_finding("invalid", MANIFEST, None, e) for e in tool.errors]
    outbound_ok = bool(tool.manifest.get("outbound")) is True
    for name, text in tool.files.items():
        secret_lines: set[int] = set()
        for pattern, what in _SECRETS:
            for m in pattern.finditer(text):
                line = _line_of(text, m.start())
                if line in secret_lines or (what == "a hard-coded credential" and _PLACEHOLDER.search(m.group(2))):
                    continue
                secret_lines.add(line)
                out.append(_finding("secret", name, line, f"looks like {what}"))
        outbound = list(_OUTBOUND_ANY)
        if Path(name).suffix.lower() in _CODE_SUFFIXES:
            outbound += _OUTBOUND_CODE
        if not outbound_ok:
            for pattern, what in outbound:
                m = pattern.search(text)
                if m:
                    out.append(_finding("outbound", name, _line_of(text, m.start()),
                                        f"{what}, but the manifest says outbound: false"))
        for pattern, what in _ESCALATION:
            m = pattern.search(text)
            if m:
                out.append(_finding("permission", name, _line_of(text, m.start()), what))
        # The shell guardrails (pos.guard.commands) line by line: bypasses always,
        # outbound commands unless declared.
        for i, line in enumerate(text.splitlines(), 1):
            c = commands.classify(line)
            if c.category == "bypass":
                out.append(_finding("bypass", name, i, c.reason))
            elif c.category == "outbound" and not outbound_ok:
                out.append(_finding("outbound", name, i, f"{c.reason}, but the manifest says outbound: false"))
    needed = set(tool.manifest.get("permissions_needed") or [])
    have = owner_perms if owner_perms is not None else owner_permissions(None, str(tool.manifest.get("owner", "")))
    extra = sorted(needed - have) if "*" not in have else []
    if extra:
        out.append(_finding("permission", MANIFEST, None,
                            f"needs {', '.join(extra)}, which its owner {tool.manifest.get('owner')!r} does not have"))
    seen, unique = set(), []
    for f in out:
        key = (f["kind"], f["file"], f["line"], f["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def describe(f: dict) -> str:
    where = f["file"] + (f":{f['line']}" if f.get("line") else "")
    return f"{f['kind']}: {where}: {f['detail']}"


# ------------------------------------------------------------------ the deployer hook

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def _tool_at(repo: Path, rev: str, name: str) -> Tool | None:
    rel = f"{SHARED_DIR}/{name}"
    paths = [p for p in _git(repo, "ls-tree", "-r", "--name-only", rev, "--", rel).splitlines() if p]
    if not paths:
        return None  # removed in this commit
    files = {}
    for p in paths[:MAX_FILES]:
        data = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{p}"], check=True,
                              capture_output=True).stdout[:MAX_FILE_BYTES]
        if b"\0" not in data:
            files[p[len(rel) + 1:]] = data.decode("utf-8", errors="replace")
    return make_tool(name, "shared", rel, files)


def check_range(repo: Path, old: str, new: str) -> list[str]:
    """Guard review of every commit in old..new that touches shared/tools/.
    Returns problems in plain words; any problem refuses the range."""
    from .guard import gitcheck

    problems = []
    for sha in reversed(gitcheck.commits_in_range(repo, old, new)):
        names = sorted({p.split("/")[2] for p in gitcheck.changed_paths(repo, sha)
                        if p.startswith(SHARED_DIR + "/") and len(p.split("/")) >= 4})
        for name in names:
            tool = _tool_at(repo, sha, name)
            if tool is None:
                continue
            problems += [f"{sha[:10]} {SHARED_DIR}/{name}: {describe(f)}" for f in review(tool)]
    return problems


def run_tests(tool: Tool, root: Path, timeout: int = 300) -> tuple[bool, str]:
    """Run a tool's tests: a test file inside its folder (python or shell), or a command."""
    folder = root / tool.path
    spec = str(tool.manifest.get("tests") or "").strip()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if spec in tool.files and spec.endswith(".py"):
        cmd, shell = [sys.executable, spec], False
    elif spec in tool.files and spec.endswith(".sh"):
        cmd, shell = ["sh", spec], False
    else:
        cmd, shell = spec, True
    p = subprocess.run(cmd, cwd=folder, shell=shell, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p.returncode == 0, (p.stdout + p.stderr)[-4000:]


# ------------------------------------------------------------------ usage and publications

def _latest_publications(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT p.*, a.name AS from_name FROM tool_publications p "
                        "LEFT JOIN actors a ON a.id = p.from_agent ORDER BY p.id").fetchall()
    return {r["tool"]: _pub(r) for r in rows}


def _pub(r: sqlite3.Row) -> dict:
    return {**dict(r), "findings": json.loads(r["findings"] or "[]")}


def tools_usage(conn: sqlite3.Connection, since: str | None = None) -> dict[str, dict]:
    """Uses per tool id ("shared/<name>" or "<agent-slug>/<name>"), for HR and the retrospective."""
    rows = conn.execute(
        """SELECT tool, COUNT(*) AS uses, SUM(ok) AS ok, MAX(at) AS last_at, COUNT(DISTINCT actor_id) AS users
           FROM tool_usage WHERE at >= ? GROUP BY tool""", (since or "",)).fetchall()
    return {r["tool"]: {"uses": r["uses"], "ok": r["ok"] or 0, "failed": r["uses"] - (r["ok"] or 0),
                        "users": r["users"], "last_at": r["last_at"]} for r in rows}


def _agent_slug(conn: sqlite3.Connection, actor_id: int) -> str:
    return slug(actors.get(conn, actor_id)["name"])


def _current_run(conn: sqlite3.Connection, actor_id: int) -> int | None:
    r = conn.execute("SELECT id FROM runs WHERE actor_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                     (actor_id,)).fetchone()
    return r["id"] if r else None


def record_use(conn: sqlite3.Connection, ctx: Ctx, name: str, ok: bool = True,
               approval_id: int | None = None) -> dict:
    """Count one use of a tool. An outbound tool is ordinary work (Ú1: audited, no approval),
    unless its manifest says outbound_kind money, commitment or personal_channel: then it
    needs an approved approval per use."""
    from .guard.policy import APPROVAL_KINDS

    tool = find(name, _agent_slug(conn, ctx.actor_id))
    needs = tool.manifest.get("outbound") and tool.manifest.get("outbound_kind") in APPROVAL_KINDS
    if needs and not actors.get(conn, ctx.actor_id)["is_owner"]:
        row = conn.execute("SELECT status, requested_by FROM approvals WHERE id = ?",
                           (approval_id or 0,)).fetchone()
        if row is None or row["status"] != "approved" or row["requested_by"] != ctx.actor_id:
            raise Forbidden(f"{name} is {tool.manifest['outbound_kind']} (constitution Ú1): request_approval "
                            "before each use and pass the approved approval_id")
    run_id = ctx.run_id or _current_run(conn, ctx.actor_id)
    cur = conn.execute("INSERT INTO tool_usage (tool, actor_id, run_id, at, ok) VALUES (?, ?, ?, ?, ?)",
                       (tool.id, ctx.actor_id, run_id, now_iso(), 1 if ok else 0))
    audit.log(conn, ctx, "tool_use", "tool", cur.lastrowid, tool=tool.id, ok=ok, approval_id=approval_id,
              **({"outbound": True} if tool.manifest.get("outbound") else {}))
    return {"tool": tool.id, "recorded": True, **tools_usage(conn).get(tool.id, {})}


def publish(conn: sqlite3.Connection, ctx: Ctx, name: str) -> dict:
    """Propose a personal tool for the team. Runs the guard review and records a
    publication; the agent then commits the copy under shared/tools/ itself
    (the deployer checks it again) and the owner approves it on the Tools page."""
    me = actors.get(conn, ctx.actor_id)
    mine = [t for t in discover() if t.name == name and not t.shared]
    tool = next((t for t in mine if t.scope == slug(me["name"])), None)
    if tool is None and me["is_owner"] and len(mine) == 1:
        tool = mine[0]
    if tool is None:
        raise NotFound(f"no personal tool {name} (it lives in agents/{slug(me['name'])}/tools/{name}/)")
    manifest = {**tool.manifest, "visibility": "team"}
    target = f"{SHARED_DIR}/{name}"
    candidate = make_tool(name, "shared", target, {**tool.files, MANIFEST: json.dumps(manifest, indent=2) + "\n"})
    findings = review(candidate, owner_permissions(conn, str(manifest.get("owner", ""))))
    existing = next((t for t in discover() if t.shared and t.name == name), None)
    if existing and _version_key(str(manifest.get("version"))) <= _version_key(str(existing.manifest.get("version"))):
        findings.append(_finding("invalid", MANIFEST, None,
                                 f"version must be higher than the shared {existing.manifest.get('version')}"))
    status = "rejected" if findings else "pending"
    cur = conn.execute(
        "INSERT INTO tool_publications (tool, from_agent, version, status, findings, created_at, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, ctx.actor_id, str(manifest.get("version")), status, json.dumps(findings), now_iso(),
         now_iso() if findings else None))
    audit.log(conn, ctx, "tool_publish", "tool_publication", cur.lastrowid, tool=name, status=status,
              findings=len(findings))
    out = {"publication": get_publication(conn, cur.lastrowid), "findings": findings, "status": status}
    if findings:
        out["next_steps"] = "Fix the findings in your personal tool, raise its version, and call tools_publish again."
    else:
        out["next_steps"] = (
            f"On your branch: copy {tool.path}/ to {target}/, write the tool.json below (visibility team), "
            f"commit with the trailer 'Tool-Publication: {cur.lastrowid}'. The deployer runs the constitution check, "
            "this guard review and the tool's tests before it reaches main; the owner approves it on the Tools page.")
        out["copy"] = {"from": tool.path, "to": target, "tool.json": json.dumps(manifest, indent=2) + "\n"}
    return out


def _version_key(v: str) -> tuple:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def get_publication(conn: sqlite3.Connection, pub_id: int) -> dict:
    r = conn.execute("SELECT p.*, a.name AS from_name FROM tool_publications p "
                     "LEFT JOIN actors a ON a.id = p.from_agent WHERE p.id = ?", (pub_id,)).fetchone()
    if r is None:
        raise NotFound(f"publication {pub_id}")
    return _pub(r)


def publications(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT p.*, a.name AS from_name FROM tool_publications p "
                        "LEFT JOIN actors a ON a.id = p.from_agent ORDER BY p.status != 'pending', p.id DESC LIMIT ?",
                        (limit,)).fetchall()
    return [_pub(r) for r in rows]


def decide(conn: sqlite3.Connection, ctx: Ctx, pub_id: int, approve: bool) -> dict:
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("only the owner decides tool publications")
    pub = get_publication(conn, pub_id)
    if pub["status"] != "pending":
        raise Forbidden(f"publication {pub_id} is already {pub['status']}")
    conn.execute("UPDATE tool_publications SET status = ?, decided_at = ? WHERE id = ?",
                 ("approved" if approve else "rejected", now_iso(), pub_id))
    audit.log(conn, ctx, "tool_publication_" + ("approve" if approve else "reject"), "tool_publication", pub_id,
              tool=pub["tool"])
    sync_publications(conn)
    return get_publication(conn, pub_id)


def sync_publications(conn: sqlite3.Connection, tools: list[Tool] | None = None) -> None:
    """An approved publication whose version is now in shared/tools/ is published."""
    shared = {t.name: t for t in (tools if tools is not None else discover()) if t.shared}
    for r in conn.execute("SELECT id, tool, version FROM tool_publications WHERE status = 'approved'").fetchall():
        t = shared.get(r["tool"])
        if t and str(t.manifest.get("version")) == r["version"]:
            conn.execute("UPDATE tool_publications SET status = 'published' WHERE id = ?", (r["id"],))


# ------------------------------------------------------------------ views

def _blocked(t: Tool, pubs: dict[str, dict]) -> bool:
    """The owner rejected this shared version: it is listed but not mounted."""
    p = pubs.get(t.name)
    return bool(t.shared and p and p["status"] == "rejected" and p["version"] == t.manifest.get("version")
                and not p["findings"])


def overview(conn: sqlite3.Connection) -> list[dict]:
    """Every tool for the Tools page: manifest, usage, live review, last publication."""
    tools = discover()
    sync_publications(conn, tools)
    usage = tools_usage(conn)
    pubs = _latest_publications(conn)
    out = []
    for t in tools:
        findings = review(t, owner_permissions(conn, str(t.manifest.get("owner", ""))))
        out.append({**t.summary(), "usage": usage.get(t.id, {"uses": 0, "ok": 0, "failed": 0, "users": 0,
                                                              "last_at": None}),
                    "review": {"ok": not findings, "findings": findings},
                    "publication": pubs.get(t.name), "blocked": _blocked(t, pubs)})
    return out


def _entry_text(t: Tool) -> str | None:
    return t.files.get(t.entry)


def detail(conn: sqlite3.Connection, name: str, scope: str | None = None) -> dict:
    items = [x for x in overview(conn) if x["name"] == name and (scope is None or x["scope"] == scope)]
    if not items:
        raise NotFound(f"tool {name}")
    item = sorted(items, key=lambda x: x["scope"] != "shared")[0]
    t = next(t for t in discover() if t.id == item["id"])
    pubs = [p for p in publications(conn, 200) if p["tool"] == name]
    return {**item, "manifest": t.manifest, "entry_content": _entry_text(t), "files": sorted(t.files),
            "publications": pubs}


def list_for(conn: sqlite3.Connection, ctx: Ctx, scope: str = "all") -> list[dict]:
    """What a member sees over MCP: their personal tools and the shared ones.
    The owner sees everyone's personal tools too."""
    if scope not in ("mine", "shared", "all"):
        raise ToolError("scope is mine, shared or all")
    me = actors.get(conn, ctx.actor_id)
    own = slug(me["name"])
    usage = tools_usage(conn)
    out = []
    for t in discover():
        visible = t.shared or t.scope == own or (me["is_owner"] and scope == "all")
        wanted = {"mine": t.scope == own, "shared": t.shared, "all": True}[scope]
        if visible and wanted:
            out.append({**t.summary(), "uses": usage.get(t.id, {}).get("uses", 0)})
    return out


def get_for(conn: sqlite3.Connection, ctx: Ctx, name: str) -> dict:
    me = actors.get(conn, ctx.actor_id)
    t = find(name, slug(me["name"]))
    out = {**t.summary(), "manifest": t.manifest}
    if t.manifest.get("kind") in ("skill", "script"):
        out["entry_content"] = _entry_text(t)
    return out


def for_agent(conn: sqlite3.Connection, actor_id: int) -> list[dict]:
    """The tools a worker mounts for this agent: its own personal tools and the
    shared tools whose permissions it has. Tools with guard findings, and
    shared versions the owner rejected, are left out."""
    tools = discover()
    sync_publications(conn, tools)
    pubs = _latest_publications(conn)
    me = actors.get(conn, actor_id)
    own = slug(me["name"])
    have = {"*"} if me["is_owner"] else set(json.loads(me["permissions"] or "[]"))
    out = []
    for t in tools:
        if not (t.scope == own or t.shared) or t.errors or _blocked(t, pubs):
            continue
        needed = set(t.manifest.get("permissions_needed") or [])
        if "*" not in have and not needed <= have:
            continue
        if review(t, owner_permissions(conn, str(t.manifest.get("owner", "")))):
            continue
        out.append({**t.summary(), "entry_path": f"{t.path}/{t.entry}",
                    "content": _entry_text(t) if t.manifest.get("kind") == "skill" else None})
    return out


# ------------------------------------------------------------------ MCP

def register_mcp(mcp, session) -> None:
    """tools_list, tools_get, tools_publish, tools_record_use on the `pos` MCP server."""
    from typing import Literal

    from mcp.server.mcpserver import Context

    from . import tasks

    def _wrap(fn):
        try:
            return fn()
        except ToolError as e:
            raise tasks.Invalid(str(e)) from e

    @mcp.tool(description="The tool library: your personal tools and the team's shared tools (scripts, MCP "
                          "tools, skills). scope: mine, shared or all.")
    def tools_list(ctx: Context, scope: Literal["mine", "shared", "all"] = "all") -> list[dict]:
        with session(ctx, "tools_list", scope=scope) as (conn, c):
            return _wrap(lambda: list_for(conn, c, scope))

    @mcp.tool(description="One tool: its manifest, and the text of its entry for skills and scripts. Your "
                          "personal tool of that name wins over the shared one.")
    def tools_get(ctx: Context, name: str) -> dict:
        with session(ctx, "tools_get", name=name) as (conn, c):
            return get_for(conn, c, name)

    @mcp.tool(description="Propose one of your personal tools (agents/<you>/tools/<name>/) for the team. Runs "
                          "the guard review (secrets, undeclared outbound calls, permissions) and records a "
                          "publication; if clean, commit the copy to shared/tools/<name>/ on your branch as told.")
    def tools_publish(ctx: Context, name: str) -> dict:
        with session(ctx, "tools_publish", name=name) as (conn, c):
            return _wrap(lambda: publish(conn, c, name))

    @mcp.tool(description="Record that you used a tool and whether it helped (ok). HR and the retrospective "
                          "read these counts. A tool with outbound: true is audited; one whose outbound_kind is money, "
                          "commitment or personal_channel needs an approved approval_id per use.")
    def tools_record_use(ctx: Context, name: str, ok: bool = True, approval_id: int | None = None) -> dict:
        with session(ctx, "tools_record_use", name=name, ok=ok) as (conn, c):
            return _wrap(lambda: record_use(conn, c, name, ok, approval_id))
