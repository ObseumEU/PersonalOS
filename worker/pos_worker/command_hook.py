"""Claude Code PreToolUse hook for Bash: every shell command an agent wants to
run is checked by PersonalOS's command guard (/api/worker/check-command,
constitution U1/U3/U4) before it runs.

    allow        -> no decision here; Claude's allow-list still applies (a command it would refuse
                    is denied here with the hint to ask the CTO: request_command_approval)
    allow + auto -> allowed (safe engineering work inside the agent's own worktree,
                    or a command the CTO approved: pos.command_policy)
    needs_owner  -> denied; PersonalOS made a task for the owner
    needs_cto    -> denied; PersonalOS made a task for the CTO (a push, a network write)
    deny         -> denied
    guard down   -> denied (fail closed)

Every denial is in PersonalOS's audit log (`command_denied`, written by the API: the request carries
`cli_allowed`, so a command the CLI's allow-list would refuse is recorded there too).

Configured by ClaudeSession (`--settings`); reads the hook event on stdin and
POS_URL / POS_AGENT_KEY from the environment.
"""

import fnmatch
import json
import os
import sys

import httpx


def decide(event: dict, post=None) -> dict | None:
    """The hook's answer for one PreToolUse event, or None to let it through."""
    if event.get("tool_name") != "Bash":
        return None
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return None
    allowed = _allowed_patterns()
    cli_ok = None if allowed is None else cli_allows(command, allowed)
    try:
        if post is None:
            # cli_allowed: the API records a command the CLI would refuse as a denial (audit command_denied)
            body = {"command": command, "cwd": event.get("cwd") or os.getcwd(),
                    "workdir": os.environ.get("POS_AGENT_WORKDIR") or None, "cli_allowed": cli_ok,
                    "run_id": int(os.environ["POS_RUN_ID"]) if os.environ.get("POS_RUN_ID", "").isdigit() else None}
            r = httpx.post(os.environ["POS_URL"].rstrip("/") + "/api/worker/check-command", json=body,
                           headers={"Authorization": f"Bearer {os.environ['POS_AGENT_KEY']}"}, timeout=30)
            r.raise_for_status()
            out = r.json()
        else:
            out = post(command)
    except Exception as e:  # noqa: BLE001 - no guard, no command
        return _deny(f"The command guard is not reachable ({type(e).__name__}); the command was not run.")
    outcome = out.get("outcome")
    if outcome == "allow":
        if out.get("auto"):
            return _allow(out.get("reason") or "allowed")
        if cli_ok is False:
            # The CLI would refuse it with a bare "requires approval" (2026-10: no agent ever asked the CTO).
            return _deny(APPROVAL_HINT)
        return None
    if outcome == "needs_cto":
        return _deny(f"{out.get('reason') or 'Needs the CTO.'} The CTO has a task for it"
                     f"{' (' + out['cto_task'] + ')' if out.get('cto_task') else ''}; do something else meanwhile.")
    if outcome == "needs_owner":
        return _deny(f"{out.get('reason') or 'Needs the owner.'} The owner has a task for it"
                     f"{' (' + out['owner_task'] + ')' if out.get('owner_task') else ''}; do something else meanwhile.")
    return _deny(out.get("reason") or f"Refused by the constitution ({out.get('rule') or 'guard'}).")


APPROVAL_HINT = ("Tento příkaz není na tvém allow-listu ani mezi automaticky povolenými (git, ruff, pytest, npm "
                 "build/test a úpravy souborů uvnitř tvého worktree). Potřebuješ-li ho, zavolej "
                 "request_command_approval(command=<přesně tento příkaz>, why=<jedna věta k čemu>): CTO ho schválí "
                 "a pak ti poběží 7 dní. Mezitím dělej něco jiného, neobcházej to jiným příkazem.")

# Commands the CLI runs without an allow-list entry (read-only).
CLI_READ_ONLY = {"ls", "pwd", "echo", "cat", "head", "tail", "grep", "rg", "wc", "sort", "uniq", "cut", "tr",
                 "true", "false", "cd", "find", "file", "stat", "diff", "which", "date", "basename", "dirname",
                 "realpath", "tree", "du", "df", "env"}


def bash_patterns(allowed_tools: list[str]) -> list[str]:
    """The Bash rules of an --allowedTools list: "*" for plain Bash, else each Bash(...) inner pattern."""
    out = []
    for t in allowed_tools or []:
        t = t.strip()
        if t == "Bash":
            out.append("*")
        elif t.startswith("Bash(") and t.endswith(")"):
            out.append(t[5:-1])
    return out


def _allowed_patterns() -> list[str] | None:
    raw = os.environ.get("POS_BASH_ALLOWED")
    if raw is None:
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return None
    return [str(p) for p in got] if isinstance(got, list) else None


def _matches(part: str, pattern: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(":*"):
        return part == pattern[:-2] or part.startswith(pattern[:-2])
    if "*" in pattern:
        return fnmatch.fnmatchcase(part, pattern)
    return part == pattern


def split_commands(command: str) -> list[str]:
    """The simple commands of a command line, split at &&, ||, ; and | outside quotes (prod 2026-10-07 18:06: the
    `;` inside `node -e "const fs=require('fs');..."` made "commands" of the script, and the allow-list's
    `node:*` was read as refusing it). The same as pos.command_policy.split_commands."""
    parts: list[str] = []
    cur: list[str] = []
    quote = None
    i = 0
    while i < len(command):
        c = command[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(command[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
            cur.append(c)
        elif c == "\\" and i + 1 < len(command):
            cur.append(c + command[i + 1])
            i += 1
        elif command.startswith(("&&", "||"), i):
            parts.append("".join(cur))
            cur = []
            i += 1
        elif c in ";|":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def cli_allows(command: str, patterns: list[str]) -> bool:
    """Would the CLI's allow-list run this command (every part of a compound command allowed)? An
    approximation erring towards "yes": an unsure answer leaves the decision to the CLI."""
    for part in split_commands(command):
        first = part.split()[0]
        if "=" in first or first in CLI_READ_ONLY:
            continue
        if not any(_matches(part, p) for p in patterns):
            return False
    return True


def _allow(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": reason}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def settings(python: str | None = None) -> dict:
    """Claude Code settings that install this hook."""
    exe = (python or sys.executable).replace("\\", "/")
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": f'"{exe}" -m pos_worker.command_hook', "timeout": 60}]}]}}


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    answer = decide(event)
    if answer:
        print(json.dumps(answer))


if __name__ == "__main__":
    main()
