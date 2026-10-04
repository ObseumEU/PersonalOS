"""The live engine: one real agent run per scenario, exactly as the worker starts it (pos_worker:
ClaudeSession, stable_prompt, build_task_prompt, the agent's own profile, model and effort, its real
guardrails), but against the mock pos MCP server and in a throw-away work folder."""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from . import fixtures, schemas
from .checks import Transcript
from .runner import AGENTS, REPO, WORKER, rm_tree

SRC = REPO / "backend" / "src"
ALWAYS_ANSWER = ("chat_send", "chat_react", "file_share", "sandbox_share")  # mcp_server.may_use


def _worker_imports():
    if str(WORKER) not in sys.path:
        sys.path.insert(0, str(WORKER))
    from pos_worker import __main__ as worker_main
    from pos_worker import claude, prompt, tools

    return worker_main, claude, prompt, tools


def org_chart() -> dict:
    """The real roster from agents/*/agent.json (what org_chart shows), for the mock server."""
    members = [{"name": "Owner", "role": "owner", "team": "board", "reports_to": None}]
    for f in sorted(AGENTS.glob("*/agent.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d.get("name") and not d.get("dormant") and d.get("enabled") is not False:
            members.append({"name": d["name"], "role": d.get("role"), "team": d.get("team"),
                            "reports_to": d.get("reports_to"), "purpose": d.get("purpose")})
    return {"members": members}


def agent_me(slug: str, all_tools: list[str], memory: str = "") -> dict:
    """What /api/worker/me serves for this agent in production: its permissions (its file's, its grants and
    the autonomy defaults every agent holds), the pos tools those allow, instructions, guardrails, profile."""
    from .. import agents_code, mcp_server
    from ..access import service as access
    from ..guard.prompt import agent_guardrails

    spec = json.loads((AGENTS / slug / "agent.json").read_text(encoding="utf-8"))
    caps = set(spec.get("permissions") or []) | set(spec.get("grants") or []) | set(access.autonomy_caps())
    if slug == "access-manager":  # granted by pos.access to the platform agent, never by its file
        caps.add(access.PERM)

    def may(tool: str) -> bool:
        perm = mcp_server.TOOL_PERMISSIONS.get(tool)
        return (perm is None or perm in caps or tool in ALWAYS_ANSWER
                or (not tool.startswith("access_") and f"tool:{tool}" in caps))

    return {"id": 0, "name": spec["name"], "kind": "agent", "permissions": sorted(caps),
            "pos_tools": [t for t in all_tools if may(t)], "all_pos_tools": all_tools,
            "instructions": (AGENTS / slug / "INSTRUCTIONS.md").read_text(encoding="utf-8"),
            "guardrails": agent_guardrails(),
            "profile": agents_code.worker_profile(spec["name"], base=AGENTS, role=spec.get("role")),
            "model": spec.get("model"), "nudges": [], "tools": [], "feedback": [], "memory": memory}


def _stream_calls(lines: list[str]) -> tuple[list[dict], dict]:
    """(built-in tool calls, the result event) from the CLI's stream-json."""
    calls, result = [], {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = ev
        msg = ev.get("message")
        if ev.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and not str(c.get("name", "")).startswith("mcp__"):
                calls.append({"name": c.get("name"), "args": c.get("input") or {}})
    return calls, result


def run(sc: dict, max_budget_usd: float, out_dir: Path, timeout_s: float = 900, keep: bool = False) -> Transcript:
    worker_main, claude, prompt, wtools = _worker_imports()
    instructions, tools, _real = schemas.tools()
    me = agent_me(sc["role"], [t["name"] for t in tools], sc.get("memory") or "")
    model = me.pop("model") or "claude-opus-5-5"  # the agent's own model (agents/<slug>/agent.json)
    me["stable_prompt"] = prompt.stable_prompt(me)

    here = out_dir / sc["id"]
    here.mkdir(parents=True, exist_ok=True)
    root = tempfile.mkdtemp(prefix="pos-eval-")
    workdir, base, sha = fixtures.make(sc.get("fixture"), root)
    canned_path, record = here / "canned.json", here / "calls.jsonl"
    record.write_text("", encoding="utf-8")
    canned_path.write_text(json.dumps({
        "instructions": instructions, "tools": tools,
        "responses": {"org_chart": org_chart(), **(sc.get("canned") or {})},
        "defaults": {"task": {**sc["task"], "assignee_name": me["name"]}, "memory": sc.get("memory") or ""},
    }, ensure_ascii=False), encoding="utf-8")
    pypath = os.pathsep.join([str(SRC), str(WORKER)])
    servers = {"pos": {"type": "stdio", "command": sys.executable,
                       "args": ["-m", "pos.evals.mock_server", str(canned_path), str(record)],
                       "env": {"PYTHONPATH": pypath, "PYTHONUTF8": "1"}}}

    # The same tool lists the worker builds (pos_worker.__main__.new_session).
    configured = wtools.tool_list(worker_main.setting(me, "claude_tools", "WORKER_CLAUDE_TOOLS", claude.DEFAULT_TOOLS))
    shown, hidden = wtools.pos_tools(me)
    allowed = [f"mcp__pos__{t}" for t in shown] + [t for t in configured if t != "mcp__pos"
                                                   and not t.startswith("mcp__pos__")]
    disallowed = [t for t in wtools.tool_list(worker_main.setting(me, "claude_disallowed", "WORKER_CLAUDE_DISALLOWED"))
                  if not t.startswith("mcp__pos__")]
    builtin, search = claude.builtin_set(
        [t.strip() for t in worker_main.setting(me, "claude_builtin", "WORKER_CLAUDE_BUILTIN").split(",") if t.strip()],
        disallowed, worker_main.tool_count(shown, me, servers))
    effort = worker_main.setting(me, "effort", "WORKER_CLAUDE_EFFORT") or None
    system_prompt = "\n\n".join(p for p in (me["guardrails"], me["stable_prompt"]) if p)
    task_prompt = prompt.build_task_prompt(me, sc["task"], sc.get("context") or [], include_guardrails=False,
                                           include_stable=False)
    (here / "prompt.md").write_text(f"{system_prompt}\n\n=== task prompt ===\n\n{task_prompt}", encoding="utf-8")

    guard = fixtures.CommandGuard()
    env = {"POS_AGENT_WORKDIR": workdir, **worker_main.git_identity(me), "POS_URL": guard.url,
           "POS_AGENT_KEY": "eval-mock", "PYTHONPATH": pypath,
           # `python` in the agent's shell is this interpreter (pytest is installed here)
           "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])}
    session = claude.ClaudeSession(
        binary=os.environ.get("CLAUDE_BIN", "claude"), workdir=workdir, model=model, system_prompt=system_prompt,
        mcp_servers=servers, allowed_tools=allowed, builtin_tools=builtin, tool_search=search,
        disallowed_tools=[f"mcp__pos__{t}" for t in hidden] + disallowed, env=env, effort=effort,
        max_budget_usd=max_budget_usd)
    timer = threading.Timer(timeout_s, session.stop)
    with guard:
        timer.start()
        try:
            for _ in session.run(task_prompt):
                pass
        finally:
            timer.cancel()
    (here / "stream.jsonl").write_text(session.jsonl, encoding="utf-8")
    (here / "command_guard.json").write_text(json.dumps(guard.seen, ensure_ascii=False, indent=1), encoding="utf-8")
    builtin_calls, result = _stream_calls(session.lines)
    pos_calls = [json.loads(x) for x in record.read_text(encoding="utf-8").splitlines() if x.strip()]
    t = Transcript(calls=pos_calls + builtin_calls, final=session.last_message, workdir=workdir, base_commits=base,
                   base_sha=sha, cost_usd=result.get("total_cost_usd"), turns=result.get("num_turns"),
                   error=session.failed)
    t.cleanup = (lambda: None) if keep else (lambda: rm_tree(root))  # after scoring (the checks read the folder)
    (here / "transcript.json").write_text(json.dumps({"calls": t.calls, "final": t.final, "cost_usd": t.cost_usd,
                                                      "turns": t.turns, "error": t.error, "workdir": workdir,
                                                      "base_commits": base, "base_sha": sha},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    return t
