"""A browser evaluation: the same three tasks through the real engines and the real browser MCP.

    python -m pos_worker.browser_eval --agent knowlage-specialist --engine claude [--tasks read,form,multi]

Runs inside the agent pool (the agent's key from POOL_KEYS_DIR/<slug>/key, never
printed). For each task it opens a run of that agent (kind `eval`, so the agent
page's trace shows the steps and screenshots), mounts the browser exactly as the
worker does (pos_worker.mounts), runs the engine's CLI with the task and the
browser guide, checks the answer and prints one JSON line: success, browser
steps, errors, tool-result tokens (text chars / 4, images ~1,600), the engine's
token totals and the time. The runs are ordinary runs: their cost is booked.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import mounts
from .claude import ClaudeSession
from .client import PosClient
from .codex import CodexSession
from .prompt import web_guide

TASKS = {
    "read": (
        "Open https://en.wikipedia.org/wiki/List_of_countries_and_dependencies_by_population and read the table. "
        "Give the five most populous countries with their population figures from the table.",
        lambda a: all(c in a.lower() for c in ("india", "china", "united states", "indonesia", "pakistan"))),
    "form": (
        "In the knowledge base's web UI at https://knowlage.obseum.cz/sources, switch the Workspace selector "
        "(a dropdown in the sidebar) to the workspace 'firma', then open the overview page ('Přehled') and report "
        "the number of documents ('Dokumenty') it shows for that workspace.",
        lambda a: "firma" in a.lower() and bool(re.search(r"\d{2,}", a))),
    "multi": (
        "On https://docs.python.org/3/ use the site's search box to search for 'asyncio.gather', open the result "
        "that documents the function asyncio.gather, and report its exact signature and the title of the page it "
        "is on. Then go back to the search results and tell how many results the search found.",
        lambda a: "gather(" in a and "return_exceptions" in a),
}
PROMPT = ("You are running a short browser evaluation for PersonalOS. Use your browser tools (no other way to "
          "reach the web). Work efficiently. End with one line that starts with 'ANSWER:' and holds the answer.\n\n"
          "Task: {task}")


def tokens_of(content) -> float:
    if isinstance(content, str):
        return len(content) / 4
    out = 0.0
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            out += len(c.get("text", "")) / 4
        elif c.get("type") == "image":
            out += 1600
    return out


def claude_stats(lines: list[str]) -> dict:
    uses, steps, errors, result_tok, usage = {}, 0, [], 0.0, {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") if isinstance(ev, dict) else None
        for c in (msg or {}).get("content") or [] if isinstance((msg or {}).get("content"), list) else []:
            if c.get("type") == "tool_use" and c.get("name", "").startswith("mcp__browser__"):
                uses[c["id"]] = c["name"].removeprefix("mcp__browser__")
                steps += 1
            if c.get("type") == "tool_result" and c.get("tool_use_id") in uses:
                result_tok += tokens_of(c.get("content"))
                if c.get("is_error"):
                    errors.append(f"{uses[c['tool_use_id']]}: {str(c.get('content'))[:160]}")
        if ev.get("type") == "result":
            u = ev.get("usage") or {}
            usage = {"input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                     "cache_read": u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0),
                     "cost_usd": ev.get("total_cost_usd")}
    return {"steps": steps, "errors": errors, "result_tokens": round(result_tok), "usage": usage,
            "tools": list(uses.values())}


def codex_stats(lines: list[str]) -> dict:
    steps, errors, result_tok, usage, tools = 0, [], 0.0, {"input": 0, "cache_read": 0, "output": 0}, []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "mcp_tool_call" and item.get("server") == "browser":
            steps += 1
            tools.append(item.get("tool"))
            res = item.get("result") or {}
            result_tok += tokens_of(res.get("content") if isinstance(res, dict) else str(res))
            if item.get("status") == "failed" or item.get("error") or (isinstance(res, dict) and res.get("is_error")):
                errors.append(f"{item.get('tool')}: {str(item.get('error') or res)[:160]}")
        if ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            usage["input"] += u.get("input_tokens", 0) - u.get("cached_input_tokens", 0)
            usage["cache_read"] += u.get("cached_input_tokens", 0)
            usage["output"] += u.get("output_tokens", 0)
    return {"steps": steps, "errors": errors, "result_tokens": round(result_tok), "usage": usage, "tools": tools}


def run_one(client: PosClient, me: dict, url: str, key: str, engine: str, name: str, model: str | None) -> dict:
    task, check = TASKS[name]
    run = client.start_run(None, kind="eval")
    me = {**me, "run_id": run["run_id"]}
    try:
        cred = client.credential_session(run["run_id"])
        cred_env = {"POS_CRED_SESSION": cred}
    except Exception:  # noqa: BLE001 - an agent without credentials
        cred_env = {}
    workdir = os.environ.get("WORKER_WORKDIR", "/work")
    where = str(Path(workdir) / (me.get("slug") or "eval"))
    Path(where).mkdir(parents=True, exist_ok=True)
    servers = mounts.browser_server(me, url, key, where, cred_env)
    guide = web_guide(me, role_only=False)  # the eval is a browser task whatever the role
    started = time.monotonic()
    if engine == "claude":
        s = ClaudeSession(workdir=where, model=model or "claude-opus-5-5", system_prompt=guide, mcp_servers=servers,
                          allowed_tools=mounts.claude_allowed(me) or ["mcp__browser"], max_budget_usd=3.0)
    else:
        s = CodexSession(workdir=where, config=[*([f'model="{model}"'] if model else []),
                                                *mounts.codex_config(servers)])
        task = guide + "\n\n" + task
    from .loop import _AliveTicker

    with _AliveTicker(client, run["run_id"]):  # a long eval is not a silent run (pos.scheduler.reap_runs)
        for _ in s.run(PROMPT.format(task=task)):
            pass
    elapsed = time.monotonic() - started
    stats = claude_stats(s.lines) if engine == "claude" else codex_stats(s.lines)
    answer = s.last_message or ""
    ok = bool(check(answer)) and not s.failed
    client.finish_run(run["run_id"], "ok" if ok else "error", s.jsonl, f"browser eval {name}: {'ok' if ok else 'failed'}")
    return {"engine": engine, "task": name, "run_id": run["run_id"], "ok": ok, "seconds": round(elapsed),
            "failed": s.failed[:200], "answer": answer[-400:], **stats}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="the agent's slug (its key in POOL_KEYS_DIR/<slug>/key)")
    ap.add_argument("--engine", choices=("claude", "codex"), required=True)
    ap.add_argument("--tasks", default="read,form,multi")
    ap.add_argument("--model")
    a = ap.parse_args()
    key = (Path(os.environ.get("POOL_KEYS_DIR", "/run/pos-keys")) / a.agent / "key").read_text().strip()
    url = os.environ.get("POS_URL", "http://localhost:8000")
    client = PosClient(url, key)
    me = {**client.me(), "slug": a.agent}
    for name in a.tasks.split(","):
        try:
            print(json.dumps(run_one(client, me, url, key, a.engine, name, a.model), ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001 - one task failing does not stop the others
            print(json.dumps({"engine": a.engine, "task": name, "ok": False, "failed": f"harness: {e}"}), flush=True)


if __name__ == "__main__":
    sys.exit(main())
