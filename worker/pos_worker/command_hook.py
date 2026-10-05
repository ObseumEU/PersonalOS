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

Configured by ClaudeSession (`--settings`); reads the hook event on stdin and
POS_URL / POS_AGENT_KEY from the environment.
"""

import fnmatch
import json
import os
import re
import sys

import httpx


def decide(event: dict, post=None) -> dict | None:
    """The hook's answer for one PreToolUse event, or None to let it through."""
    if event.get("tool_name") != "Bash":
        return None
    command = str((event.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return None
    try:
        if post is None:
            body = {"command": command, "cwd": event.get("cwd") or os.getcwd(),
                    "workdir": os.environ.get("POS_AGENT_WORKDIR") or None,
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
        allowed = _allowed_patterns()
        if allowed is not None and not cli_allows(command, allowed):
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
_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


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


def cli_allows(command: str, patterns: list[str]) -> bool:
    """Would the CLI's allow-list run this command (every part of a compound command allowed)? An
    approximation erring towards "yes": an unsure answer leaves the decision to the CLI."""
    for part in (p.strip() for p in _SEPARATORS.split(command.strip())):
        if not part:
            continue
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
