"""Talks to the server over stdio like an MCP client would."""

import json
import os
import subprocess
import sys
from pathlib import Path

server = Path(__file__).parent / "server.py"
msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "task_links", "arguments": {"text": "Fixes T-12 and T-012, see T-7."}}},
]
p = subprocess.run([sys.executable, str(server)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                   capture_output=True, text=True, env={**os.environ, "POS_WEB_URL": "https://pos.example"}, timeout=30)
replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
assert [r["id"] for r in replies] == [1, 2, 3], p.stdout + p.stderr
assert replies[0]["result"]["serverInfo"]["name"] == "task-links"
assert replies[1]["result"]["tools"][0]["name"] == "task_links"
links = json.loads(replies[2]["result"]["content"][0]["text"])
assert links == [{"ref": "T-012", "url": "https://pos.example/tasks?task=T-012"},
                 {"ref": "T-007", "url": "https://pos.example/tasks?task=T-007"}], links
print("ok")
