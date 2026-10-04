"""Claude Code PreToolUse hook for Bash: every shell command an agent wants to
run is checked by PersonalOS's command guard (/api/worker/check-command,
constitution U1/U3/U4) before it runs.

    allow        -> no decision here; Claude's allow-list still applies
    allow + auto -> allowed (safe engineering work inside the agent's own worktree,
                    or a command the CTO approved: pos.command_policy)
    needs_owner  -> denied; PersonalOS made a task for the owner
    needs_cto    -> denied; PersonalOS made a task for the CTO (a push, a network write)
    deny         -> denied
    guard down   -> denied (fail closed)

Configured by ClaudeSession (`--settings`); reads the hook event on stdin and
POS_URL / POS_AGENT_KEY from the environment.
"""

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
        return _allow(out.get("reason") or "allowed") if out.get("auto") else None
    if outcome == "needs_cto":
        return _deny(f"{out.get('reason') or 'Needs the CTO.'} The CTO has a task for it"
                     f"{' (' + out['cto_task'] + ')' if out.get('cto_task') else ''}; do something else meanwhile.")
    if outcome == "needs_owner":
        return _deny(f"{out.get('reason') or 'Needs the owner.'} The owner has a task for it"
                     f"{' (' + out['owner_task'] + ')' if out.get('owner_task') else ''}; do something else meanwhile.")
    return _deny(out.get("reason") or f"Refused by the constitution ({out.get('rule') or 'guard'}).")


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
