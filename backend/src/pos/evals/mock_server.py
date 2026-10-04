"""A mock `pos` MCP server for the eval suite: stdio, newline-delimited JSON-RPC (the MCP stdio
transport), no database and no dependency beyond the standard library.

    python -m pos.evals.mock_server <canned.json> <calls.jsonl>

canned.json (written by pos.evals.live):
    {"instructions": "...",                      the real server's instructions
     "tools": [{"name", "description", "inputSchema"}],   the real schemas (pos.evals.schemas)
     "responses": {tool: response},              per-scenario canned answers
     "defaults": {...}}                          context for the generic answers (task, ...)

A response is a JSON value returned as the tool's text, or:
    {"__error__": "msg"}                         the call fails (isError) with that message
    {"__by__": "host", "cases": {...}, "default": ...}   chosen by one argument's value
    [r1, r2, ...] under "__seq__"                one per call, the last one repeated
Every call (name + arguments) is appended to calls.jsonl before it is answered.
"""

import itertools
import json
import sys
from pathlib import Path

_ids = itertools.count(901)


def generic(name: str, args: dict, defaults: dict) -> object:
    """The answer for a tool the scenario has no canned response for: plausible and small."""
    task = defaults.get("task") or {}
    if name == "get_task":
        return {**task, "status": "working"}
    if name == "create_task":
        n = next(_ids)
        return {"ref": f"T-{n}", "id": n, "title": args.get("title"), "status": args.get("status") or "next",
                "assignee_name": args.get("assignee"), "url": f"https://pos.obseum.cz/tasks/{n}"}
    if name == "complete_task":
        return {"ref": task.get("ref"), "status": "review", "ok": True}
    if name in ("handoff_task", "task_reassign", "assign_task"):
        return {"ok": True, "ref": args.get("task") or args.get("task_id") or task.get("ref"),
                "assignee_name": args.get("to") or args.get("assignee")}
    if name in ("chat_send", "send_message", "ask_agent"):
        return {"ok": True, "message_id": next(_ids)}
    if name == "ask_owner":
        return {"ticket_id": next(_ids), "status": "open", "url": "https://pos.obseum.cz/asks"}
    if name == "request_outbound":
        held = args.get("kind") in ("money", "commitment", "personal_channel")
        return {"outbound_id": next(_ids), "status": "pending_approval" if held else "sent"}
    if name == "memory_get":
        return {"body": defaults.get("memory") or ""}
    if name == "knowledge":
        return {"passages": [], "note": "nothing found"}
    if name in ("note_create", "file_create"):
        n = next(_ids)
        return {"id": n, "url": f"https://pos.obseum.cz/notes/{n}"}
    return {"ok": True}


def answer(name: str, args: dict, canned: dict, counters: dict) -> tuple[object, bool]:
    """(the result, is it an error) for one call."""
    responses = canned.get("responses") or {}
    if name not in responses:
        return generic(name, args, canned.get("defaults") or {}), False
    r = responses[name]
    key = name
    for _ in range(8):  # nested: a choice by argument, then a sequence (or the other way round)
        if isinstance(r, dict) and "__seq__" in r:
            seq = r["__seq__"]
            i = counters.get(key, 0)
            counters[key] = i + 1
            r = seq[min(i, len(seq) - 1)]
        elif isinstance(r, dict) and "__by__" in r:
            value = str(args.get(r["__by__"], ""))
            key = f"{key}|{value}"
            r = r.get("cases", {}).get(value, r.get("default", {"ok": True}))
        else:
            break
    if isinstance(r, dict) and "__error__" in r:
        return r["__error__"], True
    return r, False


def handle(msg: dict, canned: dict, record: Path, counters: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:  # a notification (initialized, cancelled): no answer
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "pos", "title": "PersonalOS (eval mock)", "version": "0.1.0"},
                  "instructions": canned.get("instructions") or ""}
    elif method == "tools/list":
        result = {"tools": canned.get("tools") or []}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        with record.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"name": name, "args": args}, ensure_ascii=False) + "\n")
        out, error = answer(name, args, canned, counters)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "prompts/list", "resources/templates/list"):
        result = {method.split("/")[0] if "templates" not in method else "resourceTemplates": []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(canned_path: str, record_path: str) -> None:
    canned = json.loads(Path(canned_path).read_text(encoding="utf-8"))
    record = Path(record_path)
    counters: dict = {}
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            reply = handle(m, canned, record, counters) if isinstance(m, dict) else None
            if reply is not None:
                out.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                out.flush()


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2])
