"""Work folders for the scenarios: a tiny git repository per run (the Software Engineer's bug, the
Kniha Lead's plan files), with a read-only `deployer` remote like the agent pool's, and a local stand-in
for PersonalOS's command guard (/api/worker/check-command) for the agents that have Bash."""

import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GIT_ENV = {"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@evals.local",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@evals.local",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: str | Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env={**os.environ, **GIT_ENV, **(env or {})})


def make(spec: dict | None, root: str | None = None) -> tuple[str, int, str]:
    """(the work folder, commits in it, its HEAD sha) for a scenario's fixture spec:
    {"files": {path: text}, "git": true, "branch": "agent/dev", "deployer": true}. No spec: an empty folder."""
    base = Path(root or tempfile.mkdtemp(prefix="pos-eval-"))
    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    if not spec:
        return str(work), 0, ""
    for rel, text in (spec.get("files") or {}).items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    if not spec.get("git", True):
        return str(work), 0, ""
    git(work, "init", "-q", "-b", spec.get("branch") or "main")
    for k, v in (("user.name", "Fixture"), ("user.email", "fixture@evals.local"), ("commit.gpgsign", "false"),
                 ("core.autocrlf", "false"), ("core.hooksPath", os.devnull)):
        git(work, "config", k, v)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Initial fixture")
    if spec.get("deployer"):
        bare = base / "deployer.git"
        git(base, "clone", "-q", "--bare", str(work), str(bare))
        git(work, "remote", "add", "deployer", bare.as_uri())
        git(work, "fetch", "-q", "deployer")
    sha = git(work, "rev-parse", "HEAD").stdout.strip()
    return str(work), int(git(work, "rev-list", "--count", "HEAD").stdout.strip() or 0), sha


def head(workdir: str) -> str:
    return git(workdir, "rev-parse", "--short", "HEAD").stdout.strip()


def apply_builtin(workdir: str, call: dict, env: dict | None = None) -> str:
    """Replay a scripted built-in tool call (mock engine): Edit / Write a file, run a Bash command."""
    a = call["args"]
    if call["name"] in ("Edit", "Write"):
        p = Path(a["file_path"])
        p = p if p.is_absolute() else Path(workdir) / p
        if call["name"] == "Write":
            p.write_text(a["content"], encoding="utf-8", newline="\n")
            return "written"
        text = p.read_text(encoding="utf-8")
        if a["old_string"] not in text:
            return "old_string not found"
        p.write_text(text.replace(a["old_string"], a["new_string"], 1), encoding="utf-8", newline="\n")
        return "edited"
    if call["name"] == "Bash":
        r = subprocess.run(a["command"], shell=True, cwd=workdir, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env={**os.environ, **GIT_ENV, **(env or {})}, timeout=300)
        return (r.stdout + r.stderr)[-2000:]
    return ""


# ------------------------------------------------------------------ the command guard


def _posix(p: str | None) -> str | None:
    return p.replace("\\", "/") if p else p


def check_command(body: dict) -> dict:
    """What /api/worker/check-command answers, without a database: the constitution's guard
    (pos.guard.commands) and the command policy's pure parts (pos.command_policy)."""
    from .. import command_policy
    from ..guard import commands
    from ..integrations import GuardActor

    command = str(body.get("command") or "")
    d = commands.evaluate(command, GuardActor("eval-agent", False), commands.Trigger.MEMBER)
    if d.outcome.value != "allow":
        return {"outcome": d.outcome.value, "rule": d.rule, "reason": d.reason}
    why = command_policy.needs_cto(command)
    if why:
        return {"outcome": "needs_cto", "rule": "cto", "reason": f"Needs the CTO's approval: {why}."}
    if command_policy.auto_allow(command, _posix(body.get("cwd")), _posix(body.get("workdir"))):
        return {"outcome": "allow", "auto": True, "reason": "engineering work inside your worktree"}
    return {"outcome": "allow", "auto": False}


class _Guard(BaseHTTPRequestHandler):
    seen: list

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            out = check_command(body) if self.path.endswith("/check-command") else {"outcome": "deny"}
        except Exception as e:  # noqa: BLE001 - fail closed
            out = {"outcome": "deny", "reason": f"guard error: {e}"}
            body = {}
        self.seen.append({"command": body.get("command"), **out})
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


class CommandGuard:
    """A local check-command endpoint for pos_worker.command_hook (POS_URL points here)."""

    def __init__(self) -> None:
        handler = type("Guard", (_Guard,), {"seen": []})
        self.seen = handler.seen
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "CommandGuard":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
