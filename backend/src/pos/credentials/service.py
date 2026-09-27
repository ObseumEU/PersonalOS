"""Credentials: the registry, grants, resolution at execution time, the use log.

Rules (docs/CREDENTIALS.md):

- agents never see a value: they name a credential (`{{cred:github-deploy}}`
  in a command, header or body, or `credentials=[...]`), and the value is
  put in at execution time, in the one subprocess or request, and redacted
  from everything that comes back;
- using one needs an active grant `cred:<name>` (or `cred:<name>@<scope>`,
  scope = a tool, `command` or `http`, or a host). Only the owner grants
  them: pos.access treats `cred:` as owner-only, so the Access manager is
  refused, and an agent's request_access becomes an ask_owner ticket;
- every resolution is logged (agent, credential, run, task, tool, host,
  outcome). More than `max_uses_hour` uses by one agent in an hour pauses its
  grant (the owner is told and resumes it with one click);
- 1Password unreachable or not configured: every use fails closed.
"""

import hashlib
import json
import re
import secrets as pysecrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .. import actors, audit
from ..core import Ctx, Forbidden, NotFound, now_iso
from . import onepassword, store
from .redact import Redactor

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,62}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
PLACEHOLDER = re.compile(r"\{\{\s*cred:([a-z0-9][a-z0-9_.-]*)\s*\}\}")
TOOLS = ("command", "http")
PREFIX = "cred:"
MAX_BODY = 20_000
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
# A command a credential goes into is one plain command: no chaining, redirection or substitution.
SHELL_META = re.compile(r"[;&|`<>\n\r]|\$\(")
URL_HOST = re.compile(r"[a-z][a-z0-9+.-]*://(?:[^/@\s]*@)?([^/:\s\"']+)", re.I)


class CredentialError(ValueError):
    """A use or change that is refused or makes no sense; the message is safe to show an agent."""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ vocabulary

def capability(name: str, scope: str | None = None) -> str:
    return f"{PREFIX}{name}" + (f"@{scope}" if scope else "")


def parse_capability(cap: str) -> tuple[str, str | None]:
    body = cap[len(PREFIX):] if cap.startswith(PREFIX) else cap
    name, _, scope = body.partition("@")
    return name, scope or None


def status() -> dict:
    on, why = onepassword.configured()
    return {"enabled": on, "vault": onepassword.vault() or None, "reason": why or None,
            "cache_seconds": onepassword.cache_seconds()}


def _require_owner(conn: sqlite3.Connection, ctx: Ctx) -> None:
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("credentials are the owner's: only the owner adds, changes, grants and tests them")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["allowed_hosts"] = json.loads(d.get("allowed_hosts") or "[]")
    d["allowed_tools"] = json.loads(d.get("allowed_tools") or "[]")
    d["allowed_commands"] = json.loads(d.get("allowed_commands") or "[]")
    return d


def get(conn: sqlite3.Connection, key: int | str) -> dict:
    store.ensure_schema(conn)
    col = "id" if isinstance(key, int) else "name"
    r = conn.execute(f"SELECT * FROM credentials WHERE {col} = ?", (key,)).fetchone()
    if r is None:
        raise NotFound(f"no credential {key!r}")
    return _row(r)


def list_all(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict]:
    store.ensure_schema(conn)
    rows = conn.execute("SELECT * FROM credentials" + ("" if include_archived else " WHERE archived_at IS NULL")
                        + " ORDER BY name").fetchall()
    return [_row(r) for r in rows]


# ------------------------------------------------------------------ the registry (owner)

def _hosts(value) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[,\s]+", str(value or ""))
    out = []
    for h in items:
        h = str(h).strip().lower()
        if not h:
            continue
        if not re.fullmatch(r"(\*\.)?[a-z0-9.-]+(:\d+)?", h):
            raise CredentialError(f"host {h!r}: a host name like api.github.com or *.example.com")
        out.append(h)
    return sorted(set(out))


def _clean(conn: sqlite3.Connection, data: dict, partial: bool = False) -> dict:
    out: dict = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise CredentialError("name: 2-63 characters, a-z 0-9 . _ - (like github-deploy)")
        out["name"] = name
    if "op_ref" in data or not partial:
        ref = str(data.get("op_ref") or "").strip()
        vault = onepassword.vault()
        if not ref.startswith("op://") or len(ref[5:].split("/")) < 3:
            raise CredentialError("op_ref: a 1Password secret reference, op://<vault>/<item>/<field>")
        if vault and ref[5:].split("/")[0] != vault:
            raise CredentialError(f"op_ref: only items of the vault {vault!r} (the service account's vault)")
        out["op_ref"] = ref
    if "env_var" in data or not partial:
        env = (str(data.get("env_var") or "").strip() or None)
        if env and not ENV_RE.match(env):
            raise CredentialError("env_var: an upper-case variable name like GITHUB_TOKEN")
        if env and env.startswith(("POS_", "OP_")):
            raise CredentialError("env_var: POS_* and OP_* belong to the platform")
        out["env_var"] = env
    if "header" in data or not partial:
        header = (str(data.get("header") or "").strip() or None)
        if header and (":" not in header or "{value}" not in header):
            raise CredentialError("header: like 'Authorization: Bearer {value}'")
        out["header"] = header
    if "allowed_hosts" in data or not partial:
        out["allowed_hosts"] = json.dumps(_hosts(data.get("allowed_hosts")))
    if "allowed_tools" in data or not partial:
        tools = data.get("allowed_tools") or []
        tools = tools if isinstance(tools, list) else re.split(r"[,\s]+", str(tools))
        tools = sorted({t.strip() for t in tools if t.strip()})
        if set(tools) - set(TOOLS):
            raise CredentialError(f"allowed_tools: any of {TOOLS} (empty = both)")
        out["allowed_tools"] = json.dumps(tools)
    if "allowed_commands" in data or not partial:
        cmds = data.get("allowed_commands") or []
        cmds = cmds if isinstance(cmds, list) else str(cmds).splitlines()
        cmds = sorted({" ".join(str(x).split()) for x in cmds if str(x).strip()})
        if any(SHELL_META.search(x) for x in cmds):
            raise CredentialError("allowed_commands: plain command prefixes like 'git push' (no ; | & $( > <)")
        out["allowed_commands"] = json.dumps(cmds)
    if "max_uses_hour" in data or not partial:
        n = int(data.get("max_uses_hour") or 60)
        if not 1 <= n <= 10_000:
            raise CredentialError("max_uses_hour: 1 to 10 000")
        out["max_uses_hour"] = n
    for k in ("description", "notes"):
        if k in data or not partial:
            out[k] = str(data.get(k) or "")[:4000]
    return out


def add(conn: sqlite3.Connection, ctx: Ctx, data: dict) -> dict:
    """A registry entry: a name and where its value lives in 1Password. No value here, ever."""
    _require_owner(conn, ctx)
    store.ensure_schema(conn)
    f = _clean(conn, data)
    if conn.execute("SELECT 1 FROM credentials WHERE name = ?", (f["name"],)).fetchone():
        raise CredentialError(f"a credential called {f['name']} exists already")
    now = now_iso()
    cid = conn.execute(
        """INSERT INTO credentials (name, op_ref, description, env_var, header, allowed_hosts, allowed_tools,
               allowed_commands, max_uses_hour, notes, created_by, created_at, updated_at)
           VALUES (:name, :op_ref, :description, :env_var, :header, :allowed_hosts, :allowed_tools,
                   :allowed_commands, :max_uses_hour, :notes, :by, :now, :now)""", {**f, "by": ctx.actor_id, "now": now}).lastrowid
    audit.log(conn, ctx, "cred_add", "credential", cid, name=f["name"], op_ref=f["op_ref"])
    conn.commit()
    return get(conn, cid)


def update(conn: sqlite3.Connection, ctx: Ctx, cid: int, data: dict) -> dict:
    _require_owner(conn, ctx)
    before = get(conn, cid)
    f = _clean(conn, {k: v for k, v in data.items() if k != "name"}, partial=True)
    if not f:
        return before
    conn.execute(f"UPDATE credentials SET {', '.join(f'{k} = ?' for k in f)}, updated_at = ? WHERE id = ?",
                 (*f.values(), now_iso(), cid))
    if "op_ref" in f:
        onepassword.forget(before["op_ref"])
    audit.log(conn, ctx, "cred_update", "credential", cid, name=before["name"], fields=sorted(f))
    conn.commit()
    return get(conn, cid)


def archive(conn: sqlite3.Connection, ctx: Ctx, cid: int, reason: str) -> dict:
    """Out of the registry; every grant for it ends."""
    from ..access import service as access

    _require_owner(conn, ctx)
    c = get(conn, cid)
    reason = (reason or "").strip() or "credential removed"
    ended = []
    for g in grants(conn, name=c["name"]):
        access.revoke(conn, ctx, g["agent_id"], g["capability"], reason)
        ended.append(g["id"])
    conn.execute("UPDATE credentials SET archived_at = ?, updated_at = ? WHERE id = ?", (now_iso(), now_iso(), cid))
    onepassword.forget(c["op_ref"])
    audit.log(conn, ctx, "cred_archive", "credential", cid, name=c["name"], grants=ended, reason=reason)
    conn.commit()
    return {"archived": c["name"], "grants_ended": ended}


def vault_items(conn: sqlite3.Connection, ctx: Ctx) -> dict:
    """The vault's items and field names for 'add from 1Password' (never values)."""
    _require_owner(conn, ctx)
    try:
        items = onepassword.items()
    except onepassword.Unavailable as e:
        raise CredentialError(str(e)) from None
    vault = onepassword.vault()
    known = {c["op_ref"] for c in list_all(conn)}
    for it in items:
        for f in it["fields"]:
            path = f"{it['title']}/{f['section']}/{f['title']}" if f.get("section") else f"{it['title']}/{f['title']}"
            f["op_ref"] = f"op://{vault}/{path}"
            f["registered"] = f["op_ref"] in known
    return {"vault": vault, "items": items}


# ------------------------------------------------------------------ grants (owner only, via pos.access)

def grants(conn: sqlite3.Connection, *, name: str | None = None, agent_id: int | None = None,
           include_ended: bool = False, limit: int = 200) -> list[dict]:
    from ..access import store as astore

    astore.ensure_schema(conn)
    where = ["g.capability LIKE 'cred:%'"]
    args: list = []
    if name:
        where.append("(g.capability = ? OR g.capability LIKE ?)")
        args += [capability(name), capability(name) + "@%"]
    if agent_id is not None:
        where.append("g.agent_id = ?")
        args.append(agent_id)
    now = now_iso()
    if not include_ended:
        where.append(astore.ACTIVE)
        args.append(now)
    rows = conn.execute(
        f"""SELECT g.*, a.name AS agent_name, b.name AS granted_by_name FROM access_grants g
            JOIN actors a ON a.id = g.agent_id LEFT JOIN actors b ON b.id = g.granted_by
            WHERE {' AND '.join(where)} ORDER BY g.ended_at IS NOT NULL, g.id DESC LIMIT ?""",
        (*args, limit)).fetchall()
    out = []
    for r in rows:
        n, scope = parse_capability(r["capability"])
        out.append({**dict(r), "credential": n, "scope": scope,
                    "active": r["ended_at"] is None and (r["expires_at"] is None or r["expires_at"] > now)})
    return out


def grant(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, name: str, reason: str, hours: float | None = None,
          scope: str | None = None) -> dict:
    """The owner grants an agent one credential (pos.access refuses anyone else)."""
    from ..access import service as access

    _require_owner(conn, ctx)
    c = get(conn, name)
    if c["archived_at"]:
        raise CredentialError(f"{name} is archived")
    scope = (scope or "").strip().lower() or None
    if scope and scope not in TOOLS:
        _hosts([scope])
    return access.grant(conn, ctx, agent_id, capability(c["name"], scope), reason, hours)


def _covers(scope: str | None, tool: str, host: str | None) -> bool:
    if scope is None:
        return True
    if scope in TOOLS:
        return scope == tool
    return bool(host) and _host_ok(host, [scope])


def _host_ok(host: str, allowed: list[str]) -> bool:
    host = host.lower()
    for h in allowed:
        if host == h or (h.startswith("*.") and host.endswith(h[1:])):
            return True
    return False


# ------------------------------------------------------------------ requests (agent -> ask_owner -> owner)

def validate_request(conn: sqlite3.Connection, cap: str) -> None:
    """request_access(capability='cred:<name>'): the credential must exist."""
    from ..access.service import AccessError

    name, scope = parse_capability(cap)
    try:
        c = get(conn, name)
    except NotFound:
        raise AccessError(f"no credential called {name!r}: credentials_list shows the registry; the owner "
                          "adds new ones") from None
    if c["archived_at"]:
        raise AccessError(f"{name} is archived")
    if scope and scope not in TOOLS and not re.fullmatch(r"(\*\.)?[a-z0-9.-]+(:\d+)?", scope):
        raise AccessError("scope after @ is command, http or a host")


def on_access_request(conn: sqlite3.Connection, ctx: Ctx, request_id: int, cap: str, why: str,
                      task_id: int | None, hours: float | None) -> dict:
    """The Access manager may not grant credentials: the request becomes the owner's
    ask_owner ticket (with the agent's reason); the owner approves on the Přístupy page."""
    from .. import asks

    name, scope = parse_capability(cap)
    c = get(conn, name)
    me = actors.get(conn, ctx.actor_id)
    use = ", ".join(filter(None, [f"proměnná `{c['env_var']}`" if c["env_var"] else "",
                                  f"hlavička `{c['header'].split(':')[0]}`" if c["header"] else "",
                                  f"hosty {', '.join(c['allowed_hosts'])}" if c["allowed_hosts"] else ""]))
    details = "\n".join([
        f"- **Credential:** `{c['name']}`" + (f" — {c['description'].strip()[:300]}" if c["description"].strip() else ""),
        f"- **Scope:** {scope or 'any use the credential allows'}" + (f" ({use})" if use else ""),
        f"- **For:** {f'{hours:g} h' if hours else 'permanently'}",
        f"- **Access request:** #{request_id}",
        "",
        "The agent never sees the value: it is injected at execution time and redacted from the output.",
    ])
    out = asks.ask(conn, ctx, title=f"Přístup k `{c['name']}` pro {me['name']}", why=why, details=details,
                   options=["Schválit (vznikne grant)", "Zamítnout"], kind="approval", blocking=False,
                   task_id=task_id, topic=f"credential request {request_id}",
                   links=[f"[Schválit nebo zamítnout jedním klikem](/credentials?request={request_id})"],
                   after=("Approve or deny on the **Přístupy** page (the link above): approving creates the grant, "
                          f"{me['name']} gets the answer in its inbox and this ticket closes."))
    row = conn.execute("SELECT detail FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    detail = {**json.loads(row["detail"] or "{}"), "ticket_id": out["ticket_id"], "ticket_ref": out["ref"]}
    conn.execute("UPDATE access_requests SET detail = ? WHERE id = ?", (json.dumps(detail), request_id))
    return out


def open_requests(conn: sqlite3.Connection) -> list[dict]:
    from ..access import service as access

    return [r for r in access.requests(conn, "open") if (r["capability"] or "").startswith(PREFIX)]


def decide_request(conn: sqlite3.Connection, ctx: Ctx, request_id: int, decision: str, note: str,
                   hours: float | None = None) -> dict:
    """The owner's one click: grant (creates the grant) or deny; the ask_owner ticket closes."""
    from .. import tasks
    from ..access import service as access

    _require_owner(conn, ctx)
    r = conn.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    if r is None or not (r["capability"] or "").startswith(PREFIX):
        raise NotFound(f"credential request {request_id}")
    if decision not in ("grant", "deny"):
        raise CredentialError("decision: grant or deny")
    note = (note or "").strip() or ("Schváleno majitelem." if decision == "grant" else "Zamítnuto majitelem.")
    out = access.decide(conn, ctx, request_id, decision, note, hours=hours)
    ticket = json.loads(r["detail"] or "{}").get("ticket_id")
    if ticket:
        # The agent heard the decision from pos.access; the ticket closes without a second message.
        conn.execute("UPDATE owner_asks SET status = 'answered', answered_at = ? WHERE ticket_id = ?",
                     (now_iso(), ticket))
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (ticket,)).fetchone()
        if t and t["status"] != "done":
            tasks.update(conn, ctx, ticket, {"status": "done", "progress_note":
                                             f"{'Schváleno' if decision == 'grant' else 'Zamítnuto'}: {note}"[:500]})
    conn.commit()
    return out


# ------------------------------------------------------------------ resolution (execution time only)

def _log_use(conn: sqlite3.Connection, ctx: Ctx | None, c: dict | None, name: str, agent_id: int | None,
             tool: str, host: str | None, ok: bool, error: str | None, run_id: int | None,
             task_id: int | None) -> None:
    conn.execute(
        """INSERT INTO credential_uses (at, credential_id, name, agent_id, run_id, task_id, tool, host, ok, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_iso(), c["id"] if c else None, name, agent_id, run_id, task_id, tool, host, int(ok),
         (error or None) and error[:300]))
    if ctx is not None:
        audit.log(conn, ctx, "cred_use" if ok else "cred_refused", "credential", c["id"] if c else None,
                  name=name, agent=agent_id, run=run_id, task=task_id, tool=tool, host=host, error=error)


def _uses_last_hour(conn: sqlite3.Connection, agent_id: int, cid: int) -> int:
    since = _iso(_utc() - timedelta(hours=1))
    return conn.execute("SELECT COUNT(*) FROM credential_uses WHERE agent_id = ? AND credential_id = ? AND ok = 1 "
                        "AND at > ?", (agent_id, cid, since)).fetchone()[0]


def _pause(conn: sqlite3.Connection, agent_id: int, c: dict, count: int) -> list[int]:
    """Too many uses in an hour: the agent's grants for it end ('paused'); the owner resumes with one click."""
    from ..access import service as access

    rows = grants(conn, name=c["name"], agent_id=agent_id)
    why = f"{count} uses in the last hour (limit {c['max_uses_hour']})"
    now = now_iso()
    for g in rows:
        conn.execute("UPDATE access_grants SET ended_at = ?, end_kind = 'paused', end_reason = ? WHERE id = ?",
                     (now, why, g["id"]))
    access.refresh_cache(conn, agent_id)
    name = actors.get(conn, agent_id)["name"]
    ctx = Ctx(access.manager_id(conn) or actors.owner_id(conn), via="system")
    audit.log(conn, ctx, "cred_pause", "actor", agent_id, credential=c["name"], grants=[g["id"] for g in rows],
              reason=why)
    access._dm_owner(conn, f"Pozastavil jsem {name} přístup k `{c['name']}`: {why}. Pokud je to v pořádku, "
                           "obnov ho na stránce Přístupy (jedním klikem).")
    return [g["id"] for g in rows]


def command_problem(c: dict, command: str) -> str | None:
    """Why this credential may not go into this command, or None. The command must
    start with one of its allowed prefixes, be a single plain command, and name
    no host outside its allowed hosts (a token must not be pushed to another server)."""
    cmd = " ".join((command or "").split())
    if not c["allowed_commands"]:
        return "no allowed commands: the owner lists them on the credential (like 'git push')"
    if SHELL_META.search(command or ""):
        return "one plain command only (no ; | & $( ) > < or new lines)"
    if not any(cmd == p or cmd.startswith(p + " ") for p in c["allowed_commands"]):
        return f"command not allowed (only: {', '.join(c['allowed_commands'])})"
    for h in URL_HOST.findall(cmd):
        if not _host_ok(h.lower(), c["allowed_hosts"]):
            return f"host {h} is not allowed for this credential"
    return None


def resolve_for(conn: sqlite3.Connection, ctx: Ctx, names: list[str], tool: str, host: str | None = None,
                run_id: int | None = None, task_id: int | None = None,
                command: str | None = None) -> dict[str, dict]:
    """The values for one execution, all or nothing. Returns {name: {value, env_var,
    header}}; the caller injects them into one subprocess or request and forgets them.
    Every name is logged, used or refused. Raises CredentialError (safe to show)."""
    from .. import killswitch

    store.ensure_schema(conn)
    names = sorted({str(n).strip().lower() for n in names if str(n).strip()})
    if not names:
        raise CredentialError("name at least one credential")
    if tool not in TOOLS:
        raise CredentialError(f"tool: one of {TOOLS}")
    me = actors.get(conn, ctx.actor_id)
    out: dict[str, dict] = {}

    def refuse(c: dict | None, name: str, why: str):
        _log_use(conn, ctx, c, name, ctx.actor_id, tool, host, False, why, run_id, task_id)
        conn.commit()
        raise CredentialError(f"{name}: {why}")

    on, off_why = onepassword.configured()
    for name in names:
        try:
            c = get(conn, name)
        except NotFound:
            refuse(None, name, "no such credential in the registry")
        if not on:
            refuse(c, name, f"credentials are disabled ({off_why}); nothing falls back to plain text")
        if c["archived_at"]:
            refuse(c, name, "archived")
        if me["kind"] == "human":
            refuse(c, name, "credentials are injected for agents; people use 1Password directly")
        try:
            killswitch.check_agent_may_act(conn, ctx)
        except Forbidden as e:
            refuse(c, name, str(e))
        if me["paused_at"]:
            refuse(c, name, "this agent is paused")
        if c["allowed_tools"] and tool not in c["allowed_tools"]:
            refuse(c, name, f"not allowed for {tool} (only {', '.join(c['allowed_tools'])})")
        if tool == "command":
            problem = command_problem(c, command or "")
            if problem:
                refuse(c, name, problem)
        if tool == "http":
            if not c["allowed_hosts"]:
                refuse(c, name, "no allowed hosts for HTTP use: the owner sets them on the credential")
            if not host or not _host_ok(host, c["allowed_hosts"]):
                refuse(c, name, f"host {host or '?'} is not allowed (only {', '.join(c['allowed_hosts'])})")
        mine = grants(conn, name=name, agent_id=ctx.actor_id)
        if not any(_covers(g["scope"], tool, host) for g in mine):
            refuse(c, name, "no active grant: request_access(what='capability', capability='cred:" + name
                   + "', why=...) and the owner decides")
        used = _uses_last_hour(conn, ctx.actor_id, c["id"])
        if used >= c["max_uses_hour"]:
            _pause(conn, ctx.actor_id, c, used)
            refuse(c, name, f"used {used} times in the last hour (limit {c['max_uses_hour']}); the grant is paused "
                            "and the owner was told")
        try:
            value = onepassword.resolve(c["op_ref"])
        except onepassword.Unavailable as e:
            refuse(c, name, f"1Password unavailable, failing closed ({str(e)[:160]})")
        out[name] = {"value": value, "env_var": c["env_var"] or _default_env(name), "header": c["header"],
                     "credential": c}
    for name, v in out.items():
        _log_use(conn, ctx, v.pop("credential"), name, ctx.actor_id, tool, host, True, None, run_id, task_id)
    conn.commit()
    return out


def _default_env(name: str) -> str:
    return "CRED_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def test(conn: sqlite3.Connection, ctx: Ctx, cid: int) -> dict:
    """The owner's 'test resolve': OK or the error, never the value."""
    _require_owner(conn, ctx)
    c = get(conn, cid)
    try:
        onepassword.forget(c["op_ref"])
        onepassword.resolve(c["op_ref"])  # the value stays in the cache, never in the answer
        ok, err = True, None
    except onepassword.Unavailable as e:
        ok, err = False, str(e)[:300]
    _log_use(conn, ctx, c, c["name"], None, "test", None, ok, err, None, None)
    conn.commit()
    return {"ok": ok, "error": err}


def resume(conn: sqlite3.Connection, ctx: Ctx, grant_id: int, reason: str = "") -> dict:
    """A paused (or ended) credential grant again, for what was left of its time."""
    from ..access import service as access

    _require_owner(conn, ctx)
    g = conn.execute("SELECT * FROM access_grants WHERE id = ?", (grant_id,)).fetchone()
    if g is None or not g["capability"].startswith(PREFIX):
        raise NotFound(f"credential grant {grant_id}")
    hours = None
    if g["expires_at"]:
        left = (datetime.fromisoformat(g["expires_at"]) - _utc()).total_seconds() / 3600
        if left <= 0:
            raise CredentialError("that grant's time is over: grant it anew")
        hours = round(left, 2)
    return access.grant(conn, ctx, g["agent_id"], g["capability"], (reason or "").strip() or "obnoveno majitelem",
                        hours)


def uses(conn: sqlite3.Connection, *, credential_id: int | None = None, agent_id: int | None = None,
         limit: int = 100) -> list[dict]:
    store.ensure_schema(conn)
    where, args = [], []
    if credential_id is not None:
        where.append("u.credential_id = ?")
        args.append(credential_id)
    if agent_id is not None:
        where.append("u.agent_id = ?")
        args.append(agent_id)
    rows = conn.execute(
        f"""SELECT u.*, a.name AS agent_name FROM credential_uses u LEFT JOIN actors a ON a.id = u.agent_id
            {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY u.id DESC LIMIT ?""",
        (*args, limit)).fetchall()
    from ..tasks import display_id

    return [{**dict(r), "ok": bool(r["ok"]), "task_ref": display_id(r["task_id"]) if r["task_id"] else None}
            for r in rows]


# ------------------------------------------------------------------ a worker run's injection session

def open_session(conn: sqlite3.Connection, ctx: Ctx, run_id: int) -> str:
    """A token for this run's credential runner; valid while the run is live."""
    store.ensure_schema(conn)
    r = conn.execute("SELECT actor_id, status FROM runs WHERE id = ?", (run_id,)).fetchone()
    if r is None or r["actor_id"] != ctx.actor_id or r["status"] != "running":
        raise Forbidden("not a live run of yours")
    token = pysecrets.token_urlsafe(32)
    conn.execute("INSERT OR REPLACE INTO credential_sessions (run_id, agent_id, token_hash, created_at, expires_at) "
                 "VALUES (?, ?, ?, ?, ?)", (run_id, ctx.actor_id, _hash(token), now_iso(),
                                            _iso(_utc() + timedelta(hours=12))))
    conn.commit()
    return token


def check_session(conn: sqlite3.Connection, ctx: Ctx, run_id: int, token: str) -> dict:
    store.ensure_schema(conn)
    s = conn.execute("SELECT * FROM credential_sessions WHERE run_id = ?", (run_id,)).fetchone()
    run = conn.execute("SELECT actor_id, status, task_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if (s is None or run is None or s["agent_id"] != ctx.actor_id or run["actor_id"] != ctx.actor_id
            or not pysecrets.compare_digest(s["token_hash"], _hash(token or ""))):
        raise Forbidden("no credential session for this run")
    if run["status"] != "running" or s["expires_at"] <= now_iso():
        raise Forbidden("the run is over: its credential session ended")
    return {"run_id": run_id, "task_id": run["task_id"]}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ HTTP with a credential (server side)

def _fill(text: str, values: dict[str, dict]) -> str:
    def sub(m):
        n = m.group(1)
        if n not in values:
            raise CredentialError(f"{{{{cred:{n}}}}} is used but not in credentials")
        return values[n]["value"]

    return PLACEHOLDER.sub(sub, text)


def placeholders(*texts: str) -> set[str]:
    return {m.group(1) for t in texts if t for m in PLACEHOLDER.finditer(t)}


def private_host(host: str) -> bool:
    """A host on the local network (RFC 1918, link-local, loopback, *.local / *.lan / *.home.arpa)."""
    import ipaddress

    host = (host or "").lower()
    if host.endswith((".local", ".lan", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local or ip.is_loopback


def http_call(conn: sqlite3.Connection, ctx: Ctx, method: str, url: str, credentials: list[str] | None = None,
              headers: dict | None = None, body: str | None = None, task_id: int | None = None,
              transport=None) -> dict:
    """An HTTPS request with credentials put in here, in the API process: the value never
    reaches the worker or the model. `{{cred:name}}` in the URL, a header or the body is
    replaced; a named credential without a placeholder goes in its configured header.
    The response comes back redacted and marked as untrusted external content."""
    import httpx

    from ..guard.external import wrap_external

    method = (method or "GET").upper()
    if method not in HTTP_METHODS:
        raise CredentialError(f"method: one of {HTTP_METHODS}")
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host and parts.port and private_host(host):
        # Plain HTTP only inside the local network, and only to a host the credential lists
        # with its port (like Home Assistant at 192.168.1.56:8123): the host check below uses host:port.
        host = f"{host}:{parts.port}"
    elif parts.scheme != "https" or not host:
        raise CredentialError("url: https://host/... only (plain http only to a local-network host:port the "
                              "credential lists)")
    headers = {str(k): str(v) for k, v in (headers or {}).items()}
    named = {str(n).strip().lower() for n in (credentials or []) if str(n).strip()}
    used = placeholders(url, body or "", *headers.values(), *headers.keys())
    names = sorted(named | used)
    values = resolve_for(conn, ctx, names, "http", host=host, task_id=task_id)
    red = Redactor({n: v["value"] for n, v in values.items()})
    try:
        final_url = _fill(url, values)
        final_headers = {_fill(k, values): _fill(v, values) for k, v in headers.items()}
        for n in sorted(named - used):
            h = values[n]["header"]
            if not h:
                raise CredentialError(f"{n} has no header configured: put {{{{cred:{n}}}}} where it goes")
            key, _, tmpl = h.partition(":")
            final_headers[key.strip()] = tmpl.strip().replace("{value}", values[n]["value"])
        content = _fill(body, values).encode() if body is not None else None
        with httpx.Client(timeout=30, follow_redirects=False, transport=transport) as client:
            resp = client.request(method, final_url, headers=final_headers, content=content)
            text = resp.text[:MAX_BODY]
            keep = {k: resp.headers[k] for k in ("content-type", "location", "x-ratelimit-remaining") if k in resp.headers}
        status_code = resp.status_code
    except CredentialError:
        raise
    except Exception as e:  # noqa: BLE001 - a network error may quote the request: redact it too
        raise CredentialError(red(f"request failed: {type(e).__name__}: {str(e)[:300]}")) from None
    finally:
        values.clear()
    return {"status": status_code, "headers": {k: red(v) for k, v in keep.items()},
            "body": wrap_external("http", red(text), ref=red(f"{host}{parts.path}")),
            "truncated": len(text) >= MAX_BODY, "credentials": names}


# ------------------------------------------------------------------ views

def for_agent(conn: sqlite3.Connection, agent_id: int) -> list[dict]:
    """What an agent may know: the registry's names and uses, and which it holds (no values)."""
    mine = grants(conn, agent_id=agent_id)
    out = []
    for c in list_all(conn):
        g = [x for x in mine if x["credential"] == c["name"]]
        out.append({"name": c["name"], "description": c["description"][:300],
                    "use": {"env_var": c["env_var"] or _default_env(c["name"]),
                            "header": c["header"].split(":")[0] if c["header"] else None,
                            "hosts": c["allowed_hosts"], "tools": c["allowed_tools"] or list(TOOLS)},
                    "granted": bool(g), "scopes": [x["scope"] or "*" for x in g],
                    "expires_at": min((x["expires_at"] for x in g if x["expires_at"]), default=None)})
    return out


def overview(conn: sqlite3.Connection) -> dict:
    creds = list_all(conn)
    active = grants(conn)
    counts: dict[int, int] = {}
    since = _iso(_utc() - timedelta(days=1))
    for r in conn.execute("SELECT credential_id, COUNT(*) FROM credential_uses WHERE at > ? GROUP BY credential_id",
                          (since,)):
        counts[r[0]] = r[1]
    for c in creds:
        c["grants"] = [g for g in active if g["credential"] == c["name"]]
        c["uses_24h"] = counts.get(c["id"], 0)
    return {**status(), "credentials": creds, "requests": open_requests(conn),
            "paused": [g for g in grants(conn, include_ended=True, limit=100) if g["end_kind"] == "paused"][:20],
            "uses": uses(conn, limit=100)}


def detail(conn: sqlite3.Connection, cid: int) -> dict:
    c = get(conn, cid)
    return {**c, "grants": grants(conn, name=c["name"], include_ended=True),
            "uses": uses(conn, credential_id=cid, limit=200)}


def agent_view(conn: sqlite3.Connection, agent_id: int) -> dict:
    return {**status(), "grants": grants(conn, agent_id=agent_id, include_ended=True, limit=50),
            "uses": uses(conn, agent_id=agent_id, limit=50),
            "available": [c["name"] for c in list_all(conn)]}
