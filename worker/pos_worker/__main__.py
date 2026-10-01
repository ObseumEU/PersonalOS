"""Run one agent as a Codex worker.

    POS_URL=http://pos-api:8000 POS_AGENT_KEY=pos_… python -m pos_worker

Environment:
    POS_URL          PersonalOS API (inside compose: http://pos-api:8000)
    POS_MCP_URL      MCP endpoint the agent's Codex uses (default POS_URL + /mcp)
    POS_AGENT_KEY    the agent's API key (shown once when the agent is created); when empty,
                     read from POS_AGENT_KEY_FILE (default /run/pos-key/key), which the core
                     writes on the server (agents as code, pos.agents_code)
    WORKER_WORKDIR   where Codex works (default /work)
    WORKER_SANDBOX   codex sandbox mode (default workspace-write)
    WORKER_POLL      long-poll seconds (default 60)
    CODEX_BIN        codex binary (default codex); CODEX_HOME holds its login
    CLAUDE_BIN       claude binary (default claude); logged in, or CLAUDE_CODE_OAUTH_TOKEN
    WORKER_POS_TOOLS     narrows the pos MCP tools the agent sees (short names, e.g. "get_task
                     complete_task"); the base is what its permissions allow (/api/worker/me
                     pos_tools), and the inbox and team chat tools always stay
    WORKER_CLAUDE_TOOLS  other tools a Claude agent may use (default: read, edit, web); its
                     mcp__pos__ entries are ignored (WORKER_POS_TOOLS narrows those);
                     entries separated by '|' when they contain spaces
    WORKER_CLAUDE_BUILTIN  built-in tools that exist at all, comma-separated (e.g. Bash,Read,Edit)
    WORKER_CLAUDE_DISALLOWED  tools hidden from a Claude agent (saves their definitions on every turn)
    WORKER_CLAUDE_EFFORT     Claude effort level (low, medium, high, xhigh, max)
    WORKER_CLAUDE_MAX_USD    Claude cost cap per run (--max-budget-usd)
    WORKER_TRIAGE, WORKER_TRIAGE_MODEL, WORKER_TRIAGE_REPOS, WORKER_CLAUDE_MAX_USD_S
                     the cheap check before a full run and effort/cap by task size (pos_worker.triage)
    WORKER_MAX_STEPS     stop a run after this many completed steps and hand the task back (0 = no cap)
    WORKER_EXIT_IDLE_S   end the worker after this many seconds without a task (the agent pool's lazy mode)
    WORKER_CLAUDE_MCP    more MCP servers for Claude, as JSON
    BROWSER_*, PLAYWRIGHT_MCP   the guarded browser (tool:browser; pos_worker.browser_guard)
    DESKTOP_URL, DESKTOP_TOKEN  the desktop sandbox (tool:computer; pos_worker.computer, ops/desktop)
    WORKER_TOOLS_DIR PersonalOS checkout with agents/*/tools and shared/tools (default: WORKER_WORKDIR)
    WORKER_CODEX_CONFIG  extra `-c key=value` lines: more MCP servers (knowlage ingest,
                     GitHub, Gmail, Discord) with their own tokens in env vars
    WORKER_DEPLOYER_REPO the deployer's git data, mounted read-only: added as the git
                     remote `deployer` of WORKER_WORKDIR and of each clone in WORKER_DEPLOYER_CLONES
                     (comma-separated; the agent pool's /work/PersonalOS): fetch main without credentials

The agent's own "profile" and "effort" in agents/<slug>/agent.json (served in /api/worker/me)
override WORKER_POS_TOOLS, WORKER_CLAUDE_TOOLS, WORKER_CLAUDE_BUILTIN, WORKER_CLAUDE_DISALLOWED,
WORKER_CLAUDE_EFFORT, WORKER_CLAUDE_MAX_USD, WORKER_MAX_STEPS and the work folder for that agent:
the agent pool runs many agents with one environment.
"""

import json
import logging
import os
import sys

from .claude import DEFAULT_TOOLS, ClaudeSession
from .client import PosClient
from .codex import CodexSession
from .loop import Worker
from . import mounts
from . import tools as tool_library
from . import triage
from .tools import COMMS, pos_tools, tool_list  # noqa: F401 - COMMS and pos_tools are this module's API too


def extra_config() -> list[str]:
    """More MCP servers or Codex settings for this agent, one `key=value` per line
    in WORKER_CODEX_CONFIG. Example for the Mail agent (knowlage ingest):

        mcp_servers.knowlage.url="https://knowlage.example/ingest/mcp"
        mcp_servers.knowlage.bearer_token_env_var="KB_AGENT_KEY"
    """
    raw = os.environ.get("WORKER_CODEX_CONFIG", "").replace("||", chr(10))
    return [line.strip() for line in raw.splitlines() if line.strip()]


def setting(me: dict, key: str, env: str, default: str = "") -> str:
    """One worker setting: the agent's profile from its agent.json (served in /api/worker/me)
    wins over this worker's environment, so one pool container can run agents with different
    effort, tools and caps."""
    value = (me.get("profile") or {}).get(key)
    return str(value) if value is not None else os.environ.get(env, default)


CODEX_EFFORTS = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high",
                 "max": "high"}


def codex_effort(me: dict) -> list[str]:
    """The profile's effort for Codex too (it comes after WORKER_CODEX_CONFIG, so it wins)."""
    effort = CODEX_EFFORTS.get(str((me.get("profile") or {}).get("effort") or ""))
    return [f'model_reasoning_effort="{effort}"'] if effort else []


def session_workdir(me: dict, default: str) -> str:
    """The profile's work folder (a repository clone, or a read-only view under /repos); else the
    worker's own. PersonalOS only serves folders under /work or /repos (pos.agents_code)."""
    wanted = (me.get("profile") or {}).get("workdir")
    if not wanted:
        return default
    try:
        os.makedirs(wanted, exist_ok=True)
    except OSError:  # a read-only mount that exists already is fine; anything else: the default
        if not os.path.isdir(wanted):
            return default
    return wanted


def claude_extra_mcp() -> dict:
    """More MCP servers for a Claude agent as JSON in WORKER_CLAUDE_MCP, e.g.
    {"knowlage": {"type": "http", "url": "https://…/ingest/mcp", "headers": {"Authorization": "Bearer …"}}}."""

    raw = os.environ.get("WORKER_CLAUDE_MCP", "").strip()
    return json.loads(raw) if raw else {}


def run_cap(worker_cap: float | None, agent_cap: float | None) -> float | None:
    """The tighter of the worker's WORKER_CLAUDE_MAX_USD and the agent's grant; None when neither is set."""
    caps = [c for c in (worker_cap, agent_cap) if c]
    return min(caps) if caps else None


def agent_key(wait_s: float = 120) -> str:
    """POS_AGENT_KEY, else the key file the core writes (it may appear a moment
    after the API starts)."""
    import time

    key = os.environ.get("POS_AGENT_KEY", "").strip()
    path = os.environ.get("POS_AGENT_KEY_FILE", "/run/pos-key/key")
    deadline = time.monotonic() + wait_s
    while not key:
        try:
            key = open(path, encoding="utf-8").read().strip()
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"no POS_AGENT_KEY and no key file at {path}")
            time.sleep(3)
    os.environ["POS_AGENT_KEY"] = key  # Codex and the command hook read it from the environment
    return key


def deployer_remote(workdir: str) -> None:
    """Point the remote `deployer` at the deployer's checkout (WORKER_DEPLOYER_REPO,
    mounted read-only) so the agent can `git fetch deployer main` without GitHub
    credentials. Does nothing when the mount or the git repository is missing."""
    import subprocess

    repo = os.environ.get("WORKER_DEPLOYER_REPO", "").strip()
    clones = [workdir] + [c.strip() for c in os.environ.get("WORKER_DEPLOYER_CLONES", "").split(",") if c.strip()]
    clones = [c for c in dict.fromkeys(clones) if os.path.isdir(os.path.join(c, ".git"))]
    if not repo or not os.path.isdir(repo) or not clones:
        return

    def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)

    safe = git("config", "--global", "--get-all", "safe.directory").stdout.split()
    if repo not in safe and "*" not in safe:
        git("config", "--global", "--add", "safe.directory", repo)
    url = "file://" + repo
    for clone in clones:
        if git("remote", "set-url", "deployer", url, cwd=clone).returncode != 0:
            git("remote", "add", "deployer", url, cwd=clone)


def main() -> None:
    if os.environ.get("POS_CHILD_PIDFILE"):  # the real interpreter pid (a venv python.exe is only a launcher)
        open(os.environ["POS_CHILD_PIDFILE"], "w").write(str(os.getpid()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    url = os.environ.get("POS_URL", "http://localhost:8000")
    key = agent_key()
    mcp_url = os.environ.get("POS_MCP_URL", url.rstrip("/") + "/mcp")
    workdir = os.environ.get("WORKER_WORKDIR", "/work")
    os.makedirs(workdir, exist_ok=True)
    deployer_remote(workdir)

    client = PosClient(url, key)

    def credential_runner(me: dict) -> dict:
        """The credential runner (pos_worker.credentials) when the agent holds a cred:<name>
        grant: the run's session token goes to that one MCP server, not to the model's shell."""
        if not me.get("run_id"):
            return {}
        if "_cred_env" not in me:
            try:
                got = client.credential_session(me["run_id"])
            except Exception as e:  # noqa: BLE001 - an older PersonalOS, or none held: run without it
                logging.getLogger("pos_worker").info("no credential runner: %s", e)
                got = None
            me["_cred_env"] = {"POS_URL": url, "POS_AGENT_KEY": key, "POS_CRED_SESSION": got,
                               "POS_RUN_ID": str(me["run_id"]), "WORKER_WORKDIR": workdir} if got else {}
        return me["_cred_env"]

    def browser(me: dict) -> dict:
        """The guarded browser (pos_worker.browser_guard) with tool:browser (or browser:use) and the
        desktop sandbox (pos_worker.computer) with tool:computer: only with the grant (pos_worker.mounts)."""
        return {**mounts.browser_server(me, url, key, workdir, credential_runner(me)),
                **mounts.computer_server(me, url, key, workdir)}

    def new_session(engine: str, model: str | None, me: dict):
        tools = me.get("tools") or []  # the tool library: skills, MCP tools, scripts
        where = session_workdir(me, workdir)
        if me.get("task_ref") and browser(me):  # Codex passes env_vars through from this process
            os.environ["POS_TASK_ID"] = me["task_ref"]
        if engine == "claude":
            skills = tool_library.skills_text(tools)
            configured = tool_list(setting(me, "claude_tools", "WORKER_CLAUDE_TOOLS", DEFAULT_TOOLS))
            if me.get("pos_tools"):
                shown, hidden = pos_tools(me)
                allowed = [f"mcp__pos__{t}" for t in shown] + [
                    t for t in configured if t != "mcp__pos" and not t.startswith("mcp__pos__")]
            else:  # an older PersonalOS without pos_tools: the configured list as it is
                hidden, allowed = [], configured
            return ClaudeSession(
                binary=os.environ.get("CLAUDE_BIN", "claude"),
                workdir=where,
                model=model,
                # Byte-stable across runs (no task, no time), so the prompt cache reuses it.
                system_prompt="\n\n".join(p for p in (me.get("guardrails", ""), me.get("stable_prompt", ""), skills)
                                          if p),
                # The agent reaches PersonalOS through the pos MCP server, as itself.
                mcp_servers={"pos": {"type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {key}"}},
                             **browser(me), **claude_extra_mcp(), **tool_library.claude_servers(tools),
                             **({"credentials": {"type": "stdio", "command": sys.executable,
                                                 "args": ["-m", "pos_worker.credentials"],
                                                 "env": credential_runner(me)}} if credential_runner(me) else {})},
                allowed_tools=allowed + tool_library.claude_allowed(tools)
                + (mounts.claude_allowed(me) if allowed else [])
                + (["mcp__credentials"] if credential_runner(me) else []),
                builtin_tools=[t for t in setting(me, "claude_builtin", "WORKER_CLAUDE_BUILTIN").split(",") if t],
                disallowed_tools=[f"mcp__pos__{t}" for t in hidden]
                + [t for t in tool_list(setting(me, "claude_disallowed", "WORKER_CLAUDE_DISALLOWED"))
                   if not t.startswith("mcp__pos__")],
                # By the task's size when the check gave one (S: low effort, a smaller cap).
                # The cap is the lower of this worker's and the agent's max USD per run (pos.access).
                **triage.size_settings(me.get("size"), setting(me, "effort", "WORKER_CLAUDE_EFFORT") or None,
                                       run_cap(float(setting(me, "max_usd_run", "WORKER_CLAUDE_MAX_USD") or 0) or None,
                                               me.get("max_budget_usd"))),
            )
        return CodexSession(
            binary=os.environ.get("CODEX_BIN", "codex"),
            workdir=where,
            sandbox=os.environ.get("WORKER_SANDBOX", "workspace-write"),
            # PersonalOS's own servers (pos, credentials, the browser) decide themselves: pre-approved
            # for `codex exec`, which cannot ask (mounts.CODEX_EXTRA).
            config=[f'mcp_servers.pos.url="{mcp_url}"', 'mcp_servers.pos.bearer_token_env_var="POS_AGENT_KEY"',
                    *(f"mcp_servers.pos.{x}" for x in mounts.CODEX_EXTRA),
                    # a sandbox command may run up to an hour (sandbox_exec timeout ≤ 3600 s)
                    "mcp_servers.pos.tool_timeout_sec=3700", "mcp_servers.pos.startup_timeout_sec=60",
                    *([f"mcp_servers.pos.enabled_tools={json.dumps(pos_tools(me)[0])}"] if me.get("pos_tools") else []),
                    *([f'model="{model}"'] if model else []), *extra_config(), *codex_effort(me),
                    *tool_library.codex_config(tools),
                    *mounts.codex_config(browser(me)),
                    *([f'mcp_servers.credentials.command="{sys.executable.replace(chr(92), "/")}"',
                       'mcp_servers.credentials.args=["-m","pos_worker.credentials"]',
                       *(f"mcp_servers.credentials.{x}" for x in mounts.CODEX_EXTRA)]
                      + [f"mcp_servers.credentials.env.{k}={json.dumps(v)}" for k, v in credential_runner(me).items()]
                      if credential_runner(me) else [])],
        )

    def check(me: dict, task: dict) -> dict | None:
        return triage.check(me, task) if triage.enabled_for(task) else None

    ended = Worker(client, new_session, poll_wait=int(os.environ.get("WORKER_POLL", "60")),
                   max_steps=int(os.environ.get("WORKER_MAX_STEPS") or 0),
                   tools_dir=str(tool_library.tools_root(workdir)), triage=check,
                   exit_idle_s=float(os.environ.get("WORKER_EXIT_IDLE_S") or 0)).run_forever()
    if ended == "blocked":  # its run was refused: the pool gives the slot to another agent for a while
        from .pool import BLOCKED_EXIT

        sys.exit(BLOCKED_EXIT)


if __name__ == "__main__":
    main()
