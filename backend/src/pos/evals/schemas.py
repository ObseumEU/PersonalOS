"""The pos MCP tools' real names, descriptions and input schemas, for the mock server.

They come from the real server (pos.mcp_server.build without a database: building registers the
tools, nothing opens the database until a tool is called). Should that ever fail (an import that
needs the environment), a hand-written fallback covers the tools the scenarios use.
"""

import functools
from pathlib import Path

_S = {"type": "string"}
_I = {"type": "integer"}
_O = {"type": "object"}


def _schema(required: list[str], **props: dict) -> dict:
    return {"type": "object", "properties": props, "required": required}


FALLBACK = {
    "get_task": ("One task with its steps, notes and fields.", _schema(["task_id"], task_id=_S)),
    "create_task": ("Create a task.", _schema(["title"], title=_S, notes=_S, assignee=_S, do_date=_S, deadline=_S,
                                              definition_of_done=_S, priority=_I, topic=_S, project=_S)),
    "update_task": ("Change fields of a task.", _schema(["task_id", "fields"], task_id=_S, fields=_O)),
    "complete_task": ("Finish a task.", _schema(["task_id"], task_id=_S, note=_S, result_ref=_S, report=_O)),
    "handoff_task": ("Pass a task to another member with a note.", _schema(["task", "to"], task=_S, to=_S, note=_S)),
    "task_reassign": ("Reassign a task.", _schema(["task", "to"], task=_S, to=_S, note=_S)),
    "request_review": ("Hand your result in for review.", _schema(["task_id", "reviewer"], task_id=_S, reviewer=_S,
                                                                  note=_S, report=_O)),
    "report_progress": ("Report progress.", _schema(["task_id", "percent"], task_id=_S, percent=_I, message=_S)),
    "chat_send": ("Post in team chat.", _schema(["body"], body=_S, channel=_S, to=_S, reply_to=_I, task_id=_S)),
    "send_message": ("Send a message to another member.", _schema(["to", "body"], to=_S, body=_S, priority=_S,
                                                                  task_id=_S)),
    "ask_owner": ("Ask the owner (CEO only).", _schema(["title", "why"], title=_S, why=_S, details=_S,
                                                      options={"type": "array", "items": _S}, recommendation=_S,
                                                      task_id=_S, default_after_hours=_I)),
    "request_outbound": ("Send something out of PersonalOS (Ú1).", _schema(["action", "payload"], action=_S,
                                                                          payload=_O, task_id=_S, why=_S, kind=_S)),
    "knowledge": ("The company knowledge base.", _schema(["query"], query=_S, mode=_S, k=_I)),
    "memory_get": ("Your memory.", _schema([])),
    "memory_update": ("Replace your memory (the whole text).", _schema(["body"], body=_S)),
    "org_chart": ("The team.", _schema([])),
    "task_comment": ("Comment on a task.", _schema(["task_id", "body"], task_id=_S, body=_S)),
    "gmail_create_draft": ("A reply draft in the customer's Gmail thread.",
                           _schema(["thread_id", "account", "body"], thread_id=_S, account=_S, body=_S, task_id=_S)),
    "support_issue_open": ("Open a customer issue for the project's developer.",
                           _schema(["thread_id", "account", "summary"], thread_id=_S, account=_S, summary=_S,
                                   severity=_S, project=_S, customer=_S, repro_steps=_S, affected_url=_S,
                                   language=_S)),
    "metrics_snapshot": ("Host metrics.", _schema([], host=_S)),
    "loki_query": ("Query logs.", _schema(["query"], query=_S, minutes=_I, limit=_I)),
    "incident_logs": ("An incident's logs.", _schema(["task_id"], task_id=_S, container=_S)),
    "ops_runbook_list": ("Allowlisted server actions.", _schema([])),
    "ops_runbook": ("Run an allowlisted server action.", _schema(["action", "reason"], action=_S, reason=_S,
                                                                params=_O, task_id=_S)),
    "access_usage": ("Agents' spend and runs.", _schema([], agent=_S, days=_I)),
    "access_audit": ("Grants and their use.", _schema([], agent=_S, limit=_I)),
    "access_review_requests": ("Access requests.", _schema([], status=_S, agent=_S)),
    "access_decide": ("Decide an access request.", _schema(["request_id", "decision", "note"], request_id=_I,
                                                           decision=_S, note=_S)),
    "access_set_budget": ("Set an agent's budget.", _schema(["agent", "metric", "reason"], agent=_S, metric=_S,
                                                            reason=_S, amount={"type": "number"})),
    "access_resume_agent": ("Resume a paused agent.", _schema(["agent", "reason"], agent=_S, reason=_S)),
    "my_access": ("Your access.", _schema([])),
}


@functools.lru_cache(maxsize=1)
def real() -> tuple[str, list[dict]]:
    """(the server's instructions, every tool as {name, description, inputSchema}) from pos.mcp_server."""
    from .. import mcp_server

    server = mcp_server.build(Path("unused.db"))
    tools = [{"name": t.name, "description": t.description or "", "inputSchema": t.parameters}
             for t in server._tool_manager.list_tools()]
    return mcp_server.INSTRUCTIONS, tools


def tools() -> tuple[str, list[dict], bool]:
    """(instructions, tools, real?): the real ones, else the fallback."""
    try:
        instructions, out = real()
        return instructions, out, True
    except Exception:  # noqa: BLE001 - the fallback keeps the suite usable
        return "PersonalOS task list (eval mock).", [
            {"name": n, "description": d, "inputSchema": s} for n, (d, s) in FALLBACK.items()], False
