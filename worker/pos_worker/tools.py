"""Mounting the agent's tools (docs/TOOLS.md) into a session.

PersonalOS says which tools this agent may use (GET /api/worker/tools): its
personal ones and the shared ones. Skills become extra instructions, MCP
tools extra stdio MCP servers, scripts a list in the prompt. Files are looked
up under WORKER_TOOLS_DIR (a PersonalOS checkout), else the work folder.
Everything here is optional: a missing tool is skipped, never fatal.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("pos_worker")


def tool_list(raw: str) -> list[str]:
    """Split an allow-list on '|' when given (entries like "Bash(git commit:*)"
    contain spaces), else on whitespace."""
    parts = raw.split("|") if "|" in raw else raw.split()
    return [p.strip() for p in parts if p.strip()]


# Never narrowed away: talking to colleagues (standup answers, questions, handoffs), the agent's own
# memory (every prompt tells it to use it), its own computer and its files for people (docs/SANDBOX.md).
# Kept to what agents call: every listed tool's schema is paid for on every turn. The worker itself
# checks the inbox and beats the heartbeat (check_inbox, heartbeat are not needed in a narrow list);
# a sandbox_exec does what sandbox_write_file/read_file/list do.
COMMS = ("ack_message", "chat_send", "chat_read", "chat_react", "meeting_decide",
         "memory_get", "memory_update",
         "sandbox_exec", "sandbox_run_python", "sandbox_share", "file_create", "file_update", "file_share")

# Shown with any hand-in tool: the owner-report guidance needs note_get (complete_task's report check
# applies to everyone, 2026-10: Access manager 17 refusals, Software Engineer 4).
HAND_IN = ("complete_task", "request_review", "ask_owner")
REPORT_TOOLS = ("note_get",)

# Named tool sets for a profile's pos_tools ("@worker get_task ..."): one place for the common list.
PRESETS = {
    # A worker's everyday tools (~30 with COMMS): the Kniha team and the specialists; their schemas
    # go in upfront instead of all 120 (2026-10: 57k tokens of fixed prompt for a Kniha agent).
    "worker": ("get_task", "update_task", "complete_task", "request_review", "create_task", "task_comment",
               "handoff_task", "knowledge", "note_get", "note_create", "note_update", "request_outbound",
               "report_progress", "project_get", "schedule_create", "schedule_list", "search", "request_access"),
}


def expand(names: list[str]) -> list[str]:
    """Profile names with the @preset entries expanded (unknown presets are dropped)."""
    out: list[str] = []
    for n in names:
        out += list(PRESETS.get(n[1:], ())) if n.startswith("@") else [n]
    return list(dict.fromkeys(out))


def pos_tools(me: dict, narrow: str | None = None) -> tuple[list[str], list[str]]:
    """(shown, hidden) pos MCP tools: what the agent's permissions allow, narrowed
    by its profile's pos_tools (agent.json) or WORKER_POS_TOOLS; the COMMS tools
    stay when permitted, and an agent that hands in also sees the report tools."""
    permitted = list(me.get("pos_tools") or [])
    if narrow is None:
        narrow = (me.get("profile") or {}).get("pos_tools")
    raw = os.environ.get("WORKER_POS_TOOLS", "") if narrow is None else narrow
    wanted = {t.removeprefix("mcp__pos__") for t in expand(tool_list(raw))}
    if wanted and wanted & set(HAND_IN):
        wanted |= set(REPORT_TOOLS)
    shown = [t for t in permitted if not wanted or t in wanted or t in COMMS]
    everything = set(me.get("all_pos_tools") or []) | set(permitted)
    return shown, sorted(everything - set(shown))


def tools_root(workdir: str) -> Path:
    return Path(os.environ.get("WORKER_TOOLS_DIR") or workdir)


def fetch(client, root: Path) -> list:
    """The agent's tools, each with `local` set to the entry file when it exists here."""
    try:
        items = client.tools()
    except Exception as e:  # noqa: BLE001 - an old server or a network hiccup: run without tools
        log.info("no tools mounted: %s", e)
        return []
    out = []
    for t in items or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        p = root / str(t.get("entry_path") or "")
        out.append({**t, "local": str(p.resolve()) if t.get("entry_path") and p.is_file() else None})
    return out


def _server_name(t: dict) -> str:
    return "tool_" + re.sub(r"[^a-z0-9]+", "_", str(t["name"]).lower()).strip("_")


def mcp_tools(tools: list) -> list:
    """MCP tools that can be mounted: present here and not sending data out."""
    return [t for t in tools if t.get("kind") == "mcp" and t.get("local") and not t.get("outbound")]


def claude_servers(tools: list) -> dict:
    return {_server_name(t): {"type": "stdio", "command": sys.executable, "args": [t["local"]]} for t in mcp_tools(tools)}


def claude_allowed(tools: list) -> list:
    return [f"mcp__{_server_name(t)}" for t in mcp_tools(tools)]


def codex_config(tools: list) -> list:
    """`-c` overrides that add each MCP tool as a stdio server for Codex."""
    lines = []
    for t in mcp_tools(tools):
        name = _server_name(t)
        lines += [f"mcp_servers.{name}.command={json.dumps(sys.executable)}",
                  f"mcp_servers.{name}.args={json.dumps([t['local']])}"]
    return lines


def skills_text(tools: list) -> str:
    parts = [f"## Skill: {t['name']} (v{t.get('version')}, {t.get('scope')})\n\n{t['content'].strip()}"
             for t in tools if t.get("kind") == "skill" and t.get("content")]
    return ("# Your skills (tool library)\n\n" + "\n\n".join(parts)) if parts else ""


def prompt_section(tools: list, include_skills: bool) -> str:
    """The tool list for the task prompt. Codex gets the skills here too; Claude
    gets them in its system prompt."""
    if not tools:
        return ""
    lines = ["# Your tools (tool library)",
             "After using one, call tools_record_use(name, ok) so the team sees which tools help."]
    for t in tools:
        where = t.get("local") or t.get("entry_path")
        how = {"script": f"run: python {where}" if str(where).endswith(".py") else f"run: {where}",
               "mcp": f"MCP server {_server_name(t)}" if t in mcp_tools(tools) else "MCP tool (not mounted here)",
               "skill": "skill (instructions below)" if include_skills else "skill (in your instructions)"}
        line = f"- {t['name']} [{t.get('kind')}, {t.get('scope')}]: {t.get('description', '')} ({how.get(t.get('kind'), '')})"
        if t.get("outbound") and t.get("outbound_kind") in ("money", "commitment", "personal_channel"):
            line += f" {t['outbound_kind']} (Ú1): request_approval before each use."
        elif t.get("outbound"):
            line += " Sends data out of PersonalOS: ordinary work, audited (Ú1)."
        lines.append(line)
    if include_skills and skills_text(tools):
        lines += ["", skills_text(tools)]
    return "\n".join(lines)
