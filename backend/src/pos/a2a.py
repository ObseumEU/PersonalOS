"""A2A (agent-to-agent protocol), both directions (AGENTS-SPEC 6b, step 5).

Server: PersonalOS is itself an A2A agent.
    GET  /.well-known/agent-card.json
    POST /a2a   JSON-RPC 2.0: SendMessage, GetTask, CancelTask
                (A2A 1.0 names; the 0.3 names message/send, tasks/get, tasks/cancel work too)
    A message with metadata {"to": "<member>"} goes into that member's inbox
    (priority from metadata, fyi by default). Any other message becomes a
    task in PersonalOS; its A2A task id is the task ref (T-012).

Client + bridge: members with an `a2a_url` (knowlage-agent, Nexus, outside
agents) are remote. Tasks assigned to them are sent with SendMessage and
followed with GetTask by the scheduler (job a2a_sync) until they finish; the
answer comes back as the task's result for the owner's review.

Auth is a bearer key both ways. Everything is audited.
"""

import json
import os
import sqlite3
import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import actors, agents, audit, tasks
from .api_tasks import get_db
from .core import Ctx, Forbidden, NotFound, now_iso

router = APIRouter(tags=["a2a"])

# Task status in PersonalOS -> A2A 1.0 state.
STATE = {
    "inbox": "TASK_STATE_SUBMITTED", "next": "TASK_STATE_SUBMITTED", "someday": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING", "waiting": "TASK_STATE_INPUT_REQUIRED",
    "review": "TASK_STATE_COMPLETED", "done": "TASK_STATE_COMPLETED",
}


def norm_state(state: str | None) -> str:
    """'TASK_STATE_COMPLETED' / 'completed' / 'input-required' -> 'completed' / 'input_required'."""
    s = (state or "").lower().replace("task_state_", "").replace("-", "_")
    return s or "unknown"


def base_url(request: Request) -> str:
    return os.environ.get("POS_PUBLIC_URL") or str(request.base_url).rstrip("/")


def card(url: str) -> dict:
    return {
        "name": "PersonalOS",
        "description": "Personal operating system of the owner: a shared task list for people and agents. "
                       "Send a task, or send a message to a member with metadata.to.",
        "version": "0.3.0",
        "protocolVersion": "1.0",
        "url": f"{url}/a2a",
        "preferredTransport": "JSONRPC",
        "supportedInterfaces": [{"url": f"{url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "security": [{"bearer": []}],
        "skills": [
            {"id": "task", "name": "Give PersonalOS a task",
             "description": "Plain text; quick-capture syntax works (#topic !high tomorrow 30m @assignee).",
             "tags": ["tasks"], "examples": ["Prepare the Q4 report for Acme by Friday #acme !high"]},
            {"id": "message", "name": "Message a member",
             "description": "metadata.to = member name, metadata.priority = fyi | change_plan | stop.",
             "tags": ["messages"]},
        ],
    }


@router.get("/.well-known/agent-card.json")
@router.get("/.well-known/agent.json")
def agent_card(request: Request):
    return card(base_url(request))


def _text(message: dict) -> str:
    parts = message.get("parts") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")).strip()


def _task_json(t: dict, context_id: str | None = None) -> dict:
    out = {"id": t["ref"], "contextId": context_id or t["ref"],
           "status": {"state": STATE.get(t["status"], "TASK_STATE_UNSPECIFIED"), "timestamp": t["updated_at"]},
           "kind": "task"}
    if t["status"] in ("review", "done") and t.get("progress_note"):
        out["artifacts"] = [{"artifactId": f"{t['ref']}-result", "parts": [{"text": t["progress_note"]}]}]
    return out


def _error(rid, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


@router.post("/a2a")
async def a2a_rpc(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    actor_id = actors.actor_for_key(conn, key) if key else None
    try:
        req = await request.json()
    except ValueError:
        return _error(None, -32700, "parse error")
    rid = req.get("id")
    if actor_id is None:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": "unauthorized"}},
                            status_code=401)
    ctx = Ctx(actor_id, via="a2a")
    method = req.get("method", "")
    params = req.get("params") or {}
    audit.log(conn, ctx, f"a2a:{method}")
    try:
        if method in ("SendMessage", "message/send"):
            msg = params.get("message") or {}
            text = _text(msg)
            if not text:
                return _error(rid, -32602, "message has no text")
            meta = {**(params.get("metadata") or {}), **(msg.get("metadata") or {})}
            if meta.get("to"):
                target = actors.find_by_name(conn, str(meta["to"]))
                if target is None:
                    return _error(rid, -32602, f"no member called {meta['to']}")
                out = agents.send_message(conn, ctx, target["id"], text, priority=str(meta.get("priority", "fyi")))
                reply = {"messageId": str(uuid.uuid4()), "role": "ROLE_AGENT", "kind": "message",
                         "parts": [{"text": f"Delivered to {target['name']} (message {out['id']})."}]}
                return {"jsonrpc": "2.0", "id": rid, "result": {"message": reply}}
            t = tasks.capture(conn, ctx, text, source="a2a")
            conn.commit()
            return {"jsonrpc": "2.0", "id": rid, "result": {"task": _task_json(t, msg.get("contextId"))}}
        if method in ("GetTask", "tasks/get"):
            t = tasks.get(conn, ctx, tasks.parse_id(str(params.get("id", ""))))
            return {"jsonrpc": "2.0", "id": rid, "result": _task_json(t)}
        if method in ("CancelTask", "tasks/cancel"):
            tid = tasks.parse_id(str(params.get("id", "")))
            t = tasks.get(conn, ctx, tid)
            if t["created_by"] != actor_id:
                return _error(rid, -32002, "only the sender can cancel this task")
            tasks.archive(conn, ctx, tid)  # archiving, never deleting
            conn.commit()
            return {"jsonrpc": "2.0", "id": rid, "result": {**_task_json(t), "status": {"state": "TASK_STATE_CANCELED"}}}
        if method in ("SendStreamingMessage", "message/stream"):
            return _error(rid, -32004, "streaming is not supported; use SendMessage and GetTask")
        return _error(rid, -32601, f"method not found: {method}")
    except (NotFound, tasks.Invalid) as e:
        return _error(rid, -32602, str(e))
    except Forbidden as e:
        return _error(rid, -32003, str(e))


# ------------------------------------------------------------------ client

# Agent card URL -> (fetched at, JSON-RPC endpoint): the card is read once per 10 minutes, not per call.
_CARD_TTL_S = 600
_endpoints: dict[str, tuple[float, str]] = {}


class A2AClient:
    def __init__(self, url: str, key: str | None = None, http: httpx.Client | None = None, timeout: float = 30):
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}

    def endpoint(self) -> str:
        """Accept a card URL, a base URL or the JSON-RPC URL itself."""
        if self.url.endswith(".json"):
            import time

            hit = _endpoints.get(self.url)
            if hit and time.monotonic() - hit[0] < _CARD_TTL_S:
                return hit[1]
            c = self.http.get(self.url, headers=self.headers).json()
            ifaces = c.get("supportedInterfaces") or []
            url = (ifaces[0].get("url") if ifaces else None) or c["url"]
            _endpoints[self.url] = (time.monotonic(), url)
            return url
        return self.url if self.url.endswith("/a2a") or "/a2a/" in self.url else f"{self.url}/a2a"

    def call(self, method: str, params: dict) -> dict:
        r = self.http.post(self.endpoint(), headers=self.headers,
                           json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params})
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"A2A {method}: {body['error'].get('message')}")
        return body["result"]

    def send(self, text: str, context_id: str | None = None, metadata: dict | None = None) -> dict:
        msg = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]}
        if context_id:
            msg["contextId"] = context_id
        return self.call("SendMessage", {"message": msg, **({"metadata": metadata} if metadata else {})})

    def get(self, task_id: str) -> dict:
        res = self.call("GetTask", {"id": task_id})
        return res.get("task", res)


def result_text(task: dict) -> str:
    texts = []
    for a in task.get("artifacts") or []:
        texts += [p.get("text", "") for p in a.get("parts", []) if p.get("text")]
    status_msg = ((task.get("status") or {}).get("message") or {})
    texts += [p.get("text", "") for p in status_msg.get("parts", []) if p.get("text")]
    return "\n\n".join(t for t in texts if t).strip()


# ------------------------------------------------------------------ bridge (scheduler job a2a_sync)

def remote_members(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT * FROM actors WHERE a2a_url IS NOT NULL AND archived_at IS NULL").fetchall()
    return {r["id"]: dict(r) for r in rows}


def _key_for(member: dict) -> str | None:
    """Keys for remote agents come from the environment, never the database:
    POS_A2A_KEY_<NAME> (spaces and dashes as underscores)."""
    env = "POS_A2A_KEY_" + member["name"].upper().replace(" ", "_").replace("-", "_")
    return os.environ.get(env)


def sync(conn: sqlite3.Connection, http: httpx.Client | None = None) -> dict:
    """Send new tasks to remote members and collect finished ones."""
    members = remote_members(conn)
    sent, finished = [], []
    if not members:
        return {"sent": sent, "finished": finished}
    marks = ",".join("?" for _ in members)
    for t in conn.execute(
        f"""SELECT t.* FROM tasks t LEFT JOIN a2a_links l ON l.task_id = t.id
            WHERE t.assignee_id IN ({marks}) AND t.status = 'next' AND t.archived_at IS NULL AND l.task_id IS NULL""",
        list(members),
    ).fetchall():
        m = members[t["assignee_id"]]
        ctx = Ctx(m["id"], via="a2a")
        text = f"{t['title']}\n\n{t['notes']}".strip()
        try:
            res = A2AClient(m["a2a_url"], _key_for(m), http).send(text, context_id=f"pos-{t['id']}")
        except Exception as e:  # noqa: BLE001 - the remote is down or refuses: leave it queued, log why
            audit.log(conn, ctx, "a2a_send_failed", "task", t["id"], error=str(e)[:300])
            continue
        remote = res.get("task") or {}
        if not remote and res.get("message"):  # the remote answered right away
            answer = "\n".join(p.get("text", "") for p in res["message"].get("parts", []))
            tasks.claim(conn, ctx, t["id"])
            tasks.complete(conn, ctx, t["id"], answer[:4000] or "Answered.")
            finished.append(tasks.display_id(t["id"]))
            continue
        if not remote.get("id"):
            # Neither a task nor a message: nothing to follow. The task stays queued
            # (tried again next minute) instead of hanging in "working" for ever.
            audit.log(conn, ctx, "a2a_empty_reply", "task", t["id"], member=m["name"])
            continue
        conn.execute(
            "INSERT INTO a2a_links (task_id, member_id, remote_url, remote_task_id, context_id, state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (t["id"], m["id"], m["a2a_url"], remote.get("id"), remote.get("contextId"),
             norm_state((remote.get("status") or {}).get("state")), now_iso()),
        )
        tasks.claim(conn, ctx, t["id"])
        sent.append(tasks.display_id(t["id"]))

    for link in conn.execute(
        "SELECT * FROM a2a_links WHERE state NOT IN ('completed', 'failed', 'canceled', 'rejected')"
    ).fetchall():
        m = members.get(link["member_id"])
        if m is None or not link["remote_task_id"]:
            continue
        ctx = Ctx(m["id"], via="a2a")
        try:
            remote = A2AClient(link["remote_url"], _key_for(m), http).get(link["remote_task_id"])
        except Exception as e:  # noqa: BLE001
            audit.log(conn, ctx, "a2a_poll_failed", "task", link["task_id"], error=str(e)[:300])
            continue
        state = norm_state((remote.get("status") or {}).get("state"))
        conn.execute("UPDATE a2a_links SET state = ?, updated_at = ? WHERE task_id = ?", (state, now_iso(), link["task_id"]))
        if state == "completed":
            tasks.complete(conn, ctx, link["task_id"], result_text(remote)[:4000] or "Completed.")
            finished.append(tasks.display_id(link["task_id"]))
        elif state in ("failed", "rejected", "canceled"):
            tasks.update(conn, ctx, link["task_id"], {"status": "next", "progress_note": f"Remote agent: {state}"})
            tasks.assign(conn, ctx, link["task_id"], {"type": "human", "id": actors.owner_id(conn)})
        elif state in ("input_required", "auth_required"):
            tasks.update(conn, ctx, link["task_id"], {"progress_note": f"{m['name']} needs more input: {result_text(remote)[:500]}"})
    conn.commit()
    return {"sent": sent, "finished": finished}


def configure_builtin(conn: sqlite3.Connection) -> None:
    """Point the built-in subsystems at their A2A endpoints when configured."""
    for name, env in (("Knowledge agent", "POS_KNOWLAGE_A2A_URL"), ("Nexus", "POS_NEXUS_A2A_URL")):
        url = os.environ.get(env)
        conn.execute("UPDATE actors SET a2a_url = ?, runtime = ? WHERE name = ?",
                     (url or None, "a2a" if url else "builtin", name))
    conn.commit()
    from . import routing

    routing.sync_nexus_rule(conn)


def ask(conn: sqlite3.Connection, ctx: Ctx, member_name: str, question: str, wait_s: int = 60,
        http: httpx.Client | None = None, effort: int | str | None = None) -> dict:
    """Ask a remote member directly and wait a little for the answer (MCP ask_agent).

    The Knowledge agent (knowlage) gets an effort level in `metadata.effort`:
    `effort` when given, else 2 (Rychle): an agent's lookup should cost
    seconds, not the minutes of a full research run."""
    import time

    from .knowledge import AGENT_LOOKUP_EFFORT

    m = actors.find_by_name(conn, member_name)
    if m is None or not m["a2a_url"]:
        raise NotFound(f"{member_name} is not reachable over A2A")
    client = A2AClient(m["a2a_url"], _key_for(dict(m)), http)
    knowledge_agent = m["name"] == "Knowledge agent" or (
        os.environ.get("POS_KNOWLAGE_A2A_URL") and m["a2a_url"] == os.environ.get("POS_KNOWLAGE_A2A_URL"))
    if effort in (None, "") and knowledge_agent:
        effort = AGENT_LOOKUP_EFFORT
    res = client.send(question, metadata={"effort": effort} if effort not in (None, "") else None)
    audit.log(conn, ctx, "a2a_ask", "actor", m["id"])
    conn.commit()
    if res.get("message"):
        return {"answer": "\n".join(p.get("text", "") for p in res["message"].get("parts", []))}
    task = res.get("task") or {}
    deadline = time.monotonic() + wait_s
    while norm_state((task.get("status") or {}).get("state")) not in ("completed", "failed", "rejected", "canceled"):
        if time.monotonic() > deadline:
            return {"pending": True, "remote_task_id": task.get("id"),
                    "hint": "Still working; ask again later or create a task for it."}
        time.sleep(2)
        task = client.get(task["id"])
    return {"state": norm_state(task["status"]["state"]), "answer": result_text(task)}


def links(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT l.*, a.name AS member FROM a2a_links l JOIN actors a ON a.id = l.member_id ORDER BY l.updated_at DESC LIMIT 50"
    ).fetchall()
    return [{**dict(r), "task_ref": tasks.display_id(r["task_id"])} for r in rows]


def as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
