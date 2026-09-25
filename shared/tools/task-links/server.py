"""A tiny MCP server (stdio, newline-delimited JSON-RPC) with one read-only tool.

    python server.py

task_links(text): finds task refs such as T-012 in the text and returns a
link to each task in PersonalOS (POS_WEB_URL, default http://localhost:8080).
No dependencies and no network calls, so any worker can mount it.
"""

import json
import os
import re
import sys

NAME = "task-links"
VERSION = "1.0.0"
REF = re.compile(r"\bT-(\d{1,6})\b")

TOOL = {
    "name": "task_links",
    "description": "Find task refs (T-012) in a text and return a PersonalOS link for each, in order, without duplicates.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Any text: a note, a commit message, a report."}},
        "required": ["text"],
    },
}


def task_links(text: str, base: str | None = None) -> list[dict]:
    base = (base or os.environ.get("POS_WEB_URL") or "http://localhost:8080").rstrip("/")
    seen, out = set(), []
    for m in REF.finditer(text or ""):
        ref = f"T-{int(m.group(1)):03d}"
        if ref not in seen:
            seen.add(ref)
            out.append({"ref": ref, "url": f"{base}/tasks?task={ref}"})
    return out


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, the response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": NAME, "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        links = task_links(str((params.get("arguments") or {}).get("text", "")))
        result = {"content": [{"type": "text", "text": json.dumps(links)}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
