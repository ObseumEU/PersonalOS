"""The browser and the desktop as MCP servers of a run (docs/BROWSER.md).

Mounted only with the grant: `tool:browser` (or the older `browser:use`) for
the guarded browser (pos_worker.browser_guard), `tool:computer` for the desktop
sandbox (pos_worker.computer; also needs DESKTOP_URL). Each is a stdio child of
the agent's CLI, so it lives exactly as long as the run.
"""

import json
import os
import sys

BROWSER_ENV_PREFIXES = ("BROWSER_", "PLAYWRIGHT")
DESKTOP_ENV = ("DESKTOP_URL", "DESKTOP_TOKEN", "DESKTOP_QUEUE_WAIT", "DESKTOP_MAX_SCREENSHOTS", "BROWSER_APPROVAL_WAIT")


def has_browser(me: dict) -> bool:
    perms = set(me.get("permissions") or [])
    return bool(perms & {"tool:browser", "browser:use", "*"})


def has_computer(me: dict) -> bool:
    perms = set(me.get("permissions") or [])
    return bool(perms & {"tool:computer", "*"}) and bool(os.environ.get("DESKTOP_URL"))


def _run_env(me: dict, url: str, key: str, workdir: str) -> dict:
    return {"POS_URL": url, "POS_AGENT_KEY": key, "WORKER_WORKDIR": workdir,
            **({"POS_TASK_ID": str(me["task_ref"])} if me.get("task_ref") else {}),
            **({"POS_RUN_ID": str(me["run_id"])} if me.get("run_id") else {})}


def browser_server(me: dict, url: str, key: str, workdir: str, cred_env: dict | None = None) -> dict:
    """{"browser": stdio config} for Claude's --mcp-config, or {} without the grant. The run's
    credential session goes to this server only (browser_login), never to the model."""
    if not has_browser(me):
        return {}
    env = {**_run_env(me, url, key, workdir),
           **{k: v for k, v in os.environ.items() if k.startswith(BROWSER_ENV_PREFIXES)},
           **({"POS_CRED_SESSION": cred_env["POS_CRED_SESSION"]} if (cred_env or {}).get("POS_CRED_SESSION") else {})}
    return {"browser": {"type": "stdio", "command": sys.executable, "args": ["-m", "pos_worker.browser_guard"],
                        "env": env}}


def computer_server(me: dict, url: str, key: str, workdir: str) -> dict:
    if not has_computer(me):
        return {}
    env = {**_run_env(me, url, key, workdir), **{k: os.environ[k] for k in DESKTOP_ENV if os.environ.get(k)}}
    return {"computer": {"type": "stdio", "command": sys.executable, "args": ["-m", "pos_worker.computer"], "env": env}}


def claude_allowed(me: dict) -> list[str]:
    return (["mcp__browser"] if has_browser(me) else []) + (["mcp__computer"] if has_computer(me) else [])


# Codex's MCP defaults (10 s to start, 60 s per call) are too short here: a browser action may wait
# for the owner's approval (BROWSER_APPROVAL_WAIT, 900 s), the desktop for its turn (600 s), and an owner
# handoff for him to log in (pos.handoff.MAX_MINUTES, 45 min).
CODEX_TIMEOUTS = {"startup_timeout_sec": 60, "tool_timeout_sec": 3000}
# `codex exec` never asks: without default_tools_approval_mode every call of a server whose tools do not
# declare themselves read-only fails ("requires approval, but approval policy is never"); the guard is
# the approval here. required=true: the first turn waits until the server has started and listed its
# tools (otherwise Codex starts the model while the server is still starting and the run has no browser;
# startup_readiness alone did not wait, measured 2026-09-29 on Codex 0.159).
CODEX_EXTRA = ('default_tools_approval_mode="approve"', 'startup_readiness="catalog"', "required=true")


def codex_config(servers: dict) -> list[str]:
    """The same servers as Codex `-c` overrides (env values given explicitly, not passed through)."""
    lines = []
    for name, cfg in servers.items():
        lines += [f"mcp_servers.{name}.command={json.dumps(cfg['command'].replace(chr(92), '/'))}",
                  f"mcp_servers.{name}.args={json.dumps(cfg['args'])}"]
        lines += [f"mcp_servers.{name}.env.{k}={json.dumps(v)}" for k, v in cfg.get("env", {}).items()]
        lines += [f"mcp_servers.{name}.{k}={v}" for k, v in CODEX_TIMEOUTS.items()]
        lines += [f"mcp_servers.{name}.{x}" for x in CODEX_EXTRA]
    return lines
