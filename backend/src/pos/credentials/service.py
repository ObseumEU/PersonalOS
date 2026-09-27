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
import shlex
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
TOOLS = ("command", "http", "browser")  # browser: browser_login fills it into a form field (pos.browser)
PREFIX = "cred:"
MAX_BODY = 20_000
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
# A command a credential goes into is one plain command: no chaining, redirection or substitution.
SHELL_META = re.compile(r"[;&|`<>\n\r]|\$\(")
URL_HOST = re.compile(r"[a-z][a-z0-9+.-]*://(?:[^/@\s]*@)?([^/:\s\"']+)", re.I)
# A credential's env variable may be used only as its whole value: $VAR or ${VAR}. Anything
# else on a `$` (a substring/transformation like ${VAR:0:12}, ${VAR/x/y}, ${#VAR}, ${!VAR},
# arithmetic $((…)), command substitution $(…), or a bare `$`) could split or reshape the
# secret past redaction, so it is refused.
SIMPLE_VAR = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")
# The network programs whose bare argument may itself be a host (a token like evil.example.com
# with no scheme). For other programs only URLs and user@host targets count as hosts, so a
# path or a ref (README.md, origin) is not mistaken for one.
_NET_PROGRAMS = frozenset({"ssh", "scp", "sftp", "rsync", "curl", "wget", "ha_ssh", "nc", "ncat", "telnet"})
# ssh-family options that ProxyJump through another host.
_JUMP_RE = re.compile(r"^proxyjump=(.+)$", re.I)
_HOSTISH = re.compile(r"^(?:\*\.)?(?:[a-z0-9_-]+\.)+[a-z0-9_-]+(?::\d+)?$", re.I)
_IPISH = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$")


def _bad_dollar(command: str) -> bool:
    """True if `$` is used for anything but a whole-variable $VAR / ${VAR} expansion."""
    return "$" in SIMPLE_VAR.sub("", command or "")


def _basename(tok: str) -> str:
    return re.split(r"[\\/]", tok)[-1]


def _target_host(tok: str) -> str | None:
    """The host in a `[user@]host[:path]` target (ssh/scp/rsync/sftp), or None."""
    t = tok
    if "@" in t:
        t = t.rsplit("@", 1)[1]
    if t.startswith("["):  # [ipv6]:path
        end = t.find("]")
        return t[1:end].strip() or None if end > 0 else None
    t = t.split("/", 1)[0]  # a stray path after the host
    host = t.split(":", 1)[0].strip()
    return host or None


def _url_host(tok: str) -> str | None:
    m = URL_HOST.match(tok)
    return (m.group(1).split(":")[0] or None) if m else None


def command_hosts(command: str) -> list[str] | None:
    """Every host a command would talk to, or None when it cannot be determined
    (unbalanced quotes, an argument we cannot parse): the caller then fails closed.

    Handles ssh/scp/sftp/rsync `[user@]host[:path]`, `-p`/`-o …`/`-J` and ProxyJump,
    curl/wget URLs and bare hosts, git remotes (URLs and scp-style git@host:path), a
    `sshpass …` wrapper and the `ha_ssh <host>` pseudo command. Bare hostnames count
    only for network programs; for others only URLs and user@host targets do."""
    try:
        toks = shlex.split(command or "", posix=True)
    except ValueError:
        return None
    if not toks:
        return []
    i = 0
    if _basename(toks[0]) == "sshpass":
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in ("-p", "-f", "-d") and i + 1 < len(toks) else 1
    if i >= len(toks):
        return None
    prog = _basename(toks[i])
    args = toks[i + 1:]
    net = prog in _NET_PROGRAMS or prog.startswith("ssh")
    hosts: list[str] = []
    j = 0
    while j < len(args):
        a = args[j]
        low = a.lower()
        # a URL argument (any program)
        if "://" in a:
            h = _url_host(a)
            if h is None:
                return None
            hosts.append(h)
            j += 1
            continue
        # ssh-family options that carry a host
        if a in ("-J",) and j + 1 < len(args):
            hosts += _jump_hosts(args[j + 1])
            j += 2
            continue
        if a.startswith("-J"):
            hosts += _jump_hosts(a[2:])
            j += 1
            continue
        if a in ("-o",) and j + 1 < len(args):
            m = _JUMP_RE.match(args[j + 1].strip())
            if m:
                hosts += _jump_hosts(m.group(1))
            j += 2
            continue
        if a.startswith("-o"):
            m = _JUMP_RE.match(a[2:].strip())
            if m:
                hosts += _jump_hosts(m.group(1))
            j += 1
            continue
        # curl/wget proxy or connect options carry a host in their value
        if low in ("-x", "--proxy", "--connect-to", "--resolve") and j + 1 < len(args):
            h = _target_host(args[j + 1].split(":addr", 1)[0]) if low in ("--connect-to", "--resolve") \
                else _target_host(args[j + 1])
            if h:
                hosts.append(h)
            j += 2
            continue
        # any option that takes a separate value: skip the value so it is not read as a host
        if a.startswith("-"):
            j += 2 if _takes_value(prog, a) and j + 1 < len(args) else 1
            continue
        # a bare argument
        if "@" in a:  # user@host or user@host:path (git remote, ssh target)
            h = _target_host(a)
            if h:
                hosts.append(h)
        elif net:
            h = _target_host(a)
            if h and (_HOSTISH.match(h) or _IPISH.match(h)):
                hosts.append(h)
        j += 1
    return hosts


def _jump_hosts(value: str) -> list[str]:
    out = []
    for hop in value.split(","):
        h = _target_host(hop.strip())
        if h:
            out.append(h)
    return out


def _takes_value(prog: str, opt: str) -> bool:
    """Short options that consume the next token, per program (so its value is not a host)."""
    if prog in ("ssh", "scp", "sftp", "rsync") or prog.startswith("ssh"):
        return opt in ("-p", "-P", "-l", "-i", "-F", "-c", "-m", "-b", "-e", "-w", "-O", "-Q", "-D", "-L", "-R", "-W")
    if prog in ("curl", "wget"):
        return opt in ("-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
                       "-F", "--form", "-o", "--output", "-O", "-X", "--request", "-u", "--user", "-e",
                       "--referer", "-A", "--user-agent", "-b", "--cookie", "-c", "--cookie-jar", "-T",
                       "--upload-file", "-w", "--write-out", "-m", "--max-time", "--retry")
    return False


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
            raise CredentialError(f"allowed_tools: any of {TOOLS} (empty = all)")
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
    if conn.execute("SELECT 1 FROM credentials WHERE name = ? AND archived_at IS NULL", (f["name"],)).fetchone():
        raise CredentialError(f"a credential called {f['name']} exists already")
    cid = _insert(conn, ctx, f)
    conn.commit()
    return get(conn, cid)


def _insert(conn: sqlite3.Connection, ctx: Ctx, f: dict) -> int:
    """A cleaned entry (the caller commits); an archived one of the same name steps aside."""
    now = now_iso()
    old = conn.execute("SELECT id FROM credentials WHERE name = ? AND archived_at IS NOT NULL", (f["name"],)).fetchone()
    if old:
        conn.execute("UPDATE credentials SET name = name || '~' || id WHERE id = ?", (old["id"],))
    cid = conn.execute(
        """INSERT INTO credentials (name, op_ref, description, env_var, header, allowed_hosts, allowed_tools,
               allowed_commands, max_uses_hour, notes, created_by, created_at, updated_at)
           VALUES (:name, :op_ref, :description, :env_var, :header, :allowed_hosts, :allowed_tools,
                   :allowed_commands, :max_uses_hour, :notes, :by, :now, :now)""", {**f, "by": ctx.actor_id, "now": now}).lastrowid
    audit.log(conn, ctx, "cred_add", "credential", cid, name=f["name"], op_ref=f["op_ref"])
    return cid


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
    return {"vault": onepassword.vault(), "items": _vault_items(conn)}


# ------------------------------------------------------------------ grants (owner only, via pos.access)

def grants(conn: sqlite3.Connection, *, name: str | None = None, agent_id: int | None = None,
           include_ended: bool = False, limit: int = 200) -> list[dict]:
    from ..access import store as astore

    astore.ensure_schema(conn)
    where = ["g.capability LIKE 'cred:%'"]
    args: list = []
    if name:
        # GLOB, not LIKE: a credential name may contain '_', which LIKE treats as a wildcard
        # (a grant for `ha_ssh` would then match `haXssh`). GLOB's wildcards are * and ?, which
        # a credential name (a-z 0-9 . _ -) never contains, so this matches exactly name and name@<scope>.
        where.append("(g.capability = ? OR g.capability GLOB ?)")
        args += [capability(name), capability(name) + "@*"]
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
        if NAME_RE.match(name) and vault_match(conn, name, quiet=True):
            return  # in the vault, not registered yet: the owner registers and grants it in one click
        raise AccessError(f"no credential called {name!r}: credentials_list shows the registry; the owner "
                          "adds new ones (or puts it into the PersonalOS vault in 1Password)") from None
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
    extra: dict = {}
    try:
        c = get(conn, name)
    except NotFound:
        s = vault_match(conn, name, quiet=True) or {}
        extra = {"vault_item": s.get("item_id"), "vault_title": s.get("title")}
        c = {"name": name, "description": f"Zatím není v registru; v 1Password je položka „{s.get('title', '?')}“ "
                                          f"({s.get('kind_label', '?')}). Schválení ji zaregistruje a přidělí.",
             "env_var": None, "header": None, "allowed_hosts": s.get("hosts", [])}
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
    detail = {**json.loads(row["detail"] or "{}"), "ticket_id": out["ticket_id"], "ticket_ref": out["ref"], **extra}
    conn.execute("UPDATE access_requests SET detail = ? WHERE id = ?", (json.dumps(detail), request_id))
    return out


def open_requests(conn: sqlite3.Connection) -> list[dict]:
    from ..access import service as access

    out = [r for r in access.requests(conn, "open") if (r["capability"] or "").startswith(PREFIX)]
    for r in out:
        name, scope = parse_capability(r["capability"])
        row = conn.execute("SELECT id FROM credentials WHERE name = ? AND archived_at IS NULL", (name,)).fetchone()
        r.update({"credential": name, "scope": scope, "registered": row is not None,
                  "credential_id": row["id"] if row else None,
                  "suggestion": None if row else vault_match(conn, name, quiet=True)})
    return out


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
    registered = None
    if decision == "grant" and r["status"] in ("pending", "escalated"):
        registered = _register_for_request(conn, ctx, r, hours)
        if registered:
            note = f"{note} Zaregistrováno jako `{registered['primary']}`."
    out = access.decide(conn, ctx, request_id, decision, note, hours=hours)
    if registered:
        out["registered"] = registered
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
    if _bad_dollar(command or ""):
        return ("a credential variable may be used only whole, as $VAR or ${VAR} (not ${VAR:0:12}, "
                "${VAR/x/y}, ${#VAR}, ${!VAR}, $(...) or a bare $)")
    if not any(cmd == p or cmd.startswith(p + " ") for p in c["allowed_commands"]):
        return f"command not allowed (only: {', '.join(c['allowed_commands'])})"
    hosts = command_hosts(cmd)
    if hosts is None:
        return "the command's hosts cannot be determined (refused): use one plain command with clear host arguments"
    for h in hosts:
        if not _host_ok(h.lower(), c["allowed_hosts"]):
            return f"host {h} is not allowed for this credential"
    return None


# Pseudo commands that name a server-side PersonalOS tool, not a program on the worker: the
# server tool resolves the value itself (server_side=True); it is never handed to a worker.
def _server_only_commands() -> set[str]:
    from .discover import SSH_TOOLS

    return set(SSH_TOOLS)


def resolve_for(conn: sqlite3.Connection, ctx: Ctx, names: list[str], tool: str, host: str | None = None,
                run_id: int | None = None, task_id: int | None = None,
                command: str | None = None, server_side: bool = False) -> dict[str, dict]:
    """The values for one execution, all or nothing. Returns {name: {value, env_var,
    header}}; the caller injects them into one subprocess or request and forgets them.
    Every name is logged, used or refused. Raises CredentialError (safe to show).

    `server_side` is set only by PersonalOS's own in-process tools (ha_ssh, ha_ws,
    credential_http). A credential whose allowed command is a server-side pseudo tool
    (ha_ssh) is never resolved to a worker process, so the worker cannot obtain it."""
    from .. import killswitch

    server_only = _server_only_commands()

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
        if not server_side and (set(c["allowed_commands"]) & server_only):
            refuse(c, name, "this credential is used by a server-side tool only; it is never handed to a worker")
        if tool == "command":
            problem = command_problem(c, command or "")
            if problem:
                refuse(c, name, problem)
        if tool in ("http", "browser"):
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
    values = resolve_for(conn, ctx, names, "http", host=host, task_id=task_id, server_side=True)
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
            # Redact first, then truncate: truncating raw text could sever a secret past redaction.
            body = red(resp.text)
            truncated = len(body) > MAX_BODY
            body = body[:MAX_BODY]
            keep = {k: resp.headers[k] for k in ("content-type", "location", "x-ratelimit-remaining") if k in resp.headers}
        status_code = resp.status_code
    except CredentialError:
        raise
    except Exception as e:  # noqa: BLE001 - a network error may quote the request: redact it too
        raise CredentialError(red(f"request failed: {type(e).__name__}: {str(e)}")[:400]) from None
    finally:
        values.clear()
    return {"status": status_code, "headers": {k: red(v) for k, v in keep.items()},
            "body": wrap_external("http", body, ref=red(f"{host}{parts.path}")),
            "truncated": truncated, "credentials": names}


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


def kind_of(c: dict) -> str:
    """What a registered credential is, from how it is used (the card's label)."""
    cmds = " ".join(c.get("allowed_commands") or [])
    if "ssh" in cmds:
        return "ssh"
    if re.search(r"psql|pg_dump|mysql", cmds):
        return "db"
    if c.get("header"):
        return "token"
    if (c.get("allowed_tools") or []) == ["http"]:
        return "basic"
    return "generic"


def item_of(op_ref: str) -> str:
    """op://vault/ITEM/field -> ITEM (the 1Password item a credential comes from)."""
    parts = (op_ref or "")[5:].split("/")
    return parts[1] if len(parts) > 1 else op_ref


def overview(conn: sqlite3.Connection) -> dict:
    from . import discover

    creds = list_all(conn)
    active = grants(conn)
    since = _iso(_utc() - timedelta(days=1))
    stats = {r["credential_id"]: dict(r) for r in conn.execute(
        """SELECT credential_id, SUM(at > ? AND tool != 'test') AS uses_24h, SUM(at > ? AND ok = 0) AS errors_24h
           FROM credential_uses WHERE credential_id IS NOT NULL GROUP BY credential_id""", (since, since))}

    def latest(test: bool) -> dict:
        return {r["credential_id"]: {"at": r["at"], "ok": bool(r["ok"]), "error": r["error"],
                                     "agent": r["agent_name"], "tool": r["tool"]} for r in conn.execute(
            f"""SELECT u.credential_id, u.at, u.ok, u.error, u.tool, a.name AS agent_name FROM credential_uses u
                LEFT JOIN actors a ON a.id = u.agent_id WHERE u.id IN (SELECT MAX(id) FROM credential_uses
                WHERE tool {'=' if test else '!='} 'test' AND credential_id IS NOT NULL GROUP BY credential_id)""")}

    tests, lasts = latest(True), latest(False)
    roster = discover.agents(conn)
    for c in creds:
        st = stats.get(c["id"], {})
        c["grants"] = [g for g in active if g["credential"] == c["name"]]
        c["uses_24h"] = st.get("uses_24h") or 0
        c["errors_24h"] = st.get("errors_24h") or 0
        c["last_use"] = lasts.get(c["id"])
        c["last_test"] = tests.get(c["id"])
        c["kind"] = kind_of(c)
        c["item"] = item_of(c["op_ref"])
        c["companions"] = companion_grants([c])
        c["recommended"] = discover.recommend_for(c, roster)
    return {**status(), "credentials": creds, "requests": open_requests(conn),
            "paused": [g for g in grants(conn, include_ended=True, limit=100) if g["end_kind"] == "paused"][:20],
            "agents": roster, "audit": audit_groups(conn)}


def detail(conn: sqlite3.Connection, cid: int) -> dict:
    c = get(conn, cid)
    return {**c, "grants": grants(conn, name=c["name"], include_ended=True),
            "uses": uses(conn, credential_id=cid, limit=200)}


def agent_view(conn: sqlite3.Connection, agent_id: int) -> dict:
    return {**status(), "grants": grants(conn, agent_id=agent_id, include_ended=True, limit=50),
            "uses": uses(conn, agent_id=agent_id, limit=50),
            "audit": audit_groups(conn, agent_id=agent_id),
            "requests": [r for r in open_requests(conn) if r["agent_id"] == agent_id],
            "available": [c["name"] for c in list_all(conn)]}


# ------------------------------------------------------------------ discovery, one-click register + grant

DISMISSED_KEY = "credentials.dismissed"


def _vault_items(conn: sqlite3.Connection, refresh: bool = False) -> list[dict]:
    """The vault's items with each field's op:// reference and whether it is registered (never values)."""
    try:
        items = onepassword.items(refresh=refresh)
    except onepassword.Unavailable as e:
        raise CredentialError(str(e)) from None
    vault = onepassword.vault()
    known = {c["op_ref"] for c in list_all(conn)}
    for it in items:
        for f in it["fields"]:
            path = f"{it['title']}/{f['section']}/{f['title']}" if f.get("section") else f"{it['title']}/{f['title']}"
            f["op_ref"] = f"op://{vault}/{path}"
            f["registered"] = f["op_ref"] in known
        it["registered"] = any(f["registered"] for f in it["fields"])
    return items


def discover_items(conn: sqlite3.Connection, ctx: Ctx, include_hidden: bool = False, refresh: bool = False) -> dict:
    """Vault items not in the registry, each with a suggestion (rules first; the model only
    for items the rules cannot place, a few per call, cached)."""
    from .. import settings_store
    from . import discover

    _require_owner(conn, ctx)
    on, why = onepassword.configured()
    if not on:
        return {**status(), "items": [], "hidden": [], "error": why}
    try:
        items = _vault_items(conn, refresh=refresh)
    except CredentialError as e:
        return {**status(), "items": [], "hidden": [], "error": str(e)}
    dismissed = set(settings_store.get(conn, DISMISSED_KEY, []) or [])
    roster = discover.agents(conn)
    taken = {r[0] for r in conn.execute("SELECT name FROM credentials WHERE archived_at IS NULL")}
    out, hidden, budget = [], [], discover.LLM_MAX_PER_CALL
    for it in items:
        if it["registered"]:
            continue
        if it["id"] in dismissed and not include_hidden:
            hidden.append({"item_id": it["id"], "title": it["title"]})
            continue
        s = discover.suggest(conn, it, roster=roster, taken=taken, use_llm=budget > 0)
        if s["source"] == "llm" or (not s["decided"] and budget > 0):
            budget -= 1
        s["hidden"] = it["id"] in dismissed
        out.append(s)
    return {**status(), "items": out, "hidden": hidden, "error": None}


def suggest_item(conn: sqlite3.Connection, ctx: Ctx, item_id: str, kind: str | None = None) -> dict:
    """One item's suggestion again, as another kind (the owner's 'Upravit')."""
    from . import discover

    _require_owner(conn, ctx)
    it = next((i for i in _vault_items(conn) if i["id"] == item_id), None)
    if it is None:
        raise NotFound(f"no item {item_id} in the vault")
    try:
        return discover.suggest(conn, it, kind=kind or None, use_llm=False)
    except ValueError as e:
        raise CredentialError(str(e)) from None


def dismiss(conn: sqlite3.Connection, ctx: Ctx, item_id: str, hidden: bool = True) -> dict:
    from .. import settings_store

    _require_owner(conn, ctx)
    cur = [x for x in (settings_store.get(conn, DISMISSED_KEY, []) or []) if x != item_id]
    if hidden:
        cur.append(item_id)
    settings_store.put(conn, ctx, DISMISSED_KEY, cur[-500:])
    audit.log(conn, ctx, "cred_dismiss" if hidden else "cred_undismiss", "credential", None, item=item_id)
    conn.commit()
    return {"item_id": item_id, "hidden": hidden}


def vault_match(conn: sqlite3.Connection, name: str, quiet: bool = False) -> dict | None:
    """The unregistered vault item an agent means by `name`: a suggested credential of that
    name, or an item whose title says the same words. Rules only (no model)."""
    from . import discover

    try:
        items = [i for i in _vault_items(conn) if not i["registered"]]
    except CredentialError:
        if quiet:
            return None
        raise
    if not items:
        return None
    roster = discover.agents(conn)
    want = set(name.split("-")) - {""}
    for it in items:
        s = discover.suggest(conn, it, roster=roster, taken=set(), use_llm=False)
        title = discover.tokens(it["title"]) | {discover.norm(it["title"]).replace(" ", "")}
        if name in {c["name"] for c in s["credentials"]} or discover.slug(it["title"]) == name or want <= title:
            return s
    return None


def companion_grants(creds: list[dict]) -> list[str]:
    """Grants a credential needs besides itself: the dedicated tool its pseudo command names (ha_ssh)."""
    from .discover import SSH_TOOLS

    return sorted({SSH_TOOLS[cmd] for c in creds for cmd in (c.get("allowed_commands") or []) if cmd in SSH_TOOLS})


def register_and_grant(conn: sqlite3.Connection, ctx: Ctx, spec: dict) -> dict:
    """The owner's one click on a discovery card: the item's registry entries and their grants
    to the chosen agents (plus the tool a credential's pseudo command needs), all checked
    before anything is written. No value is read."""
    from ..access import service as access

    _require_owner(conn, ctx)
    store.ensure_schema(conn)
    items = spec.get("credentials") or []
    if not items:
        raise CredentialError("nothing to register: pick at least one field")
    cleaned, names = [], set()
    for c in items:
        data = {k: c.get(k) for k in ("name", "op_ref", "description", "env_var", "header", "allowed_hosts",
                                      "allowed_tools", "allowed_commands", "max_uses_hour", "notes")
                if c.get(k) is not None}
        f = _clean(conn, data)
        if f["name"] in names or conn.execute("SELECT 1 FROM credentials WHERE name = ? AND archived_at IS NULL",
                                              (f["name"],)).fetchone():
            raise CredentialError(f"a credential called {f['name']} exists already: pick another name")
        names.add(f["name"])
        cleaned.append(f)
    agent_ids = sorted({int(a) for a in spec.get("agent_ids") or []})
    for a in agent_ids:
        row = conn.execute("SELECT kind, archived_at FROM actors WHERE id = ?", (a,)).fetchone()
        if row is None or row["kind"] == "human" or row["archived_at"]:
            raise CredentialError(f"agent #{a}: not an active agent")
    hours = spec.get("hours")
    if hours is not None and not (0 < float(hours) <= 24 * 366):
        raise CredentialError("hours: between 0 and a year (empty = permanent)")
    scope = (spec.get("scope") or "").strip().lower() or None
    if scope and scope not in TOOLS:
        _hosts([scope])
    reason = (spec.get("reason") or "").strip() or "zaregistrováno a přiděleno majitelem"
    ids = [_insert(conn, ctx, f) for f in cleaned]
    conn.commit()
    made = [get(conn, i) for i in ids]
    extra = companion_grants(made)
    granted = []
    for a in agent_ids:
        for c in made:
            granted.append(access.grant(conn, ctx, a, capability(c["name"], scope), reason, hours)["grant_id"])
        for cap in extra:
            if cap not in (access.effective(conn, a) or set()):
                granted.append(access.grant(conn, ctx, a, cap, f"{reason} (nástroj pro {made[0]['name']})")["grant_id"])
    audit.log(conn, ctx, "cred_register_grant", "credential", ids[0], names=[c["name"] for c in made],
              item=spec.get("item_id"), agents=agent_ids, extra=extra, grants=granted)
    conn.commit()
    return {"credentials": made, "grants": granted, "agents": agent_ids, "extra_grants": extra}


def _register_for_request(conn: sqlite3.Connection, ctx: Ctx, r: sqlite3.Row, hours: float | None) -> dict | None:
    """A request for a credential that is only in the vault: register the item's suggestion,
    grant its other entries (and the tool they need) to the agent, and point the request at
    the registered name; access.decide then grants that one."""
    from ..access import service as access

    name, scope = parse_capability(r["capability"])
    if conn.execute("SELECT 1 FROM credentials WHERE name = ? AND archived_at IS NULL", (name,)).fetchone():
        return None
    s = vault_match(conn, name)
    if not s or not s["credentials"]:
        raise CredentialError(f"{name} is neither in the registry nor in the vault: add it to 1Password first")
    primary = next((c for c in s["credentials"] if c["name"] == name), s["credentials"][0])
    out = register_and_grant(conn, ctx, {"item_id": s["item_id"], "credentials": s["credentials"], "agent_ids": []})
    reason = f"žádost #{r['id']}"
    for c in out["credentials"]:
        if c["name"] != primary["name"]:
            access.grant(conn, ctx, r["agent_id"], capability(c["name"], scope), reason,
                         hours if hours is not None else r["hours"])
    for cap in out["extra_grants"]:
        if cap not in (access.effective(conn, r["agent_id"]) or set()):
            access.grant(conn, ctx, r["agent_id"], cap, reason)
    conn.execute("UPDATE access_requests SET capability = ? WHERE id = ?",
                 (capability(primary["name"], scope), r["id"]))
    conn.commit()
    return {"primary": primary["name"], "names": [c["name"] for c in out["credentials"]],
            "extra_grants": out["extra_grants"]}


def grant_many(conn: sqlite3.Connection, ctx: Ctx, agent_ids: list[int], names: list[str], reason: str,
               hours: float | None = None, scope: str | None = None) -> dict:
    """Přidělit on a card: every credential of the card (an SSH pair) to every picked agent,
    plus the tool their pseudo command needs. Owner only (grant checks it)."""
    from ..access import service as access

    _require_owner(conn, ctx)
    names = sorted({n.strip().lower() for n in names if n and n.strip()})
    if not names or not agent_ids:
        raise CredentialError("pick at least one agent and one credential")
    cs = [get(conn, n) for n in names]
    reason = (reason or "").strip() or "přiděleno majitelem"
    out = []
    for a in sorted(set(agent_ids)):
        for c in cs:
            out.append(grant(conn, ctx, a, c["name"], reason, hours, scope)["grant_id"])
        for cap in companion_grants(cs):
            if cap not in (access.effective(conn, a) or set()):
                out.append(access.grant(conn, ctx, a, cap, f"{reason} (nástroj pro {cs[0]['name']})")["grant_id"])
    return {"grants": out}


def revoke_many(conn: sqlite3.Connection, ctx: Ctx, grant_ids: list[int], reason: str) -> dict:
    """Odebrat: the agent's grants of a card, in one click; the ids come back for Vrátit (undo)."""
    from ..access import service as access

    _require_owner(conn, ctx)
    done = []
    for gid in sorted(set(grant_ids)):
        g = conn.execute("SELECT * FROM access_grants WHERE id = ?", (gid,)).fetchone()
        if g is None or not g["capability"].startswith(PREFIX):
            raise NotFound(f"credential grant {gid}")
        if g["ended_at"] is None:
            access.revoke_grant(conn, ctx, gid, (reason or "").strip() or "odebráno majitelem")
            done.append(gid)
    return {"revoked": done}


def restore_grants(conn: sqlite3.Connection, ctx: Ctx, grant_ids: list[int]) -> dict:
    """Vrátit (undo) after Odebrat: the same grants again, for what was left of their time."""
    return {"grants": [resume(conn, ctx, gid, "vráceno majitelem (zpět)")["grant_id"] for gid in sorted(set(grant_ids))]}


# ------------------------------------------------------------------ the audit, grouped

def audit_groups(conn: sqlite3.Connection, days: int = 7, credential_id: int | None = None,
                 agent_id: int | None = None) -> list[dict]:
    """One line per day, credential and agent: how many uses, how many refused, the last error."""
    store.ensure_schema(conn)
    where, args = ["u.at > ?"], [_iso(_utc() - timedelta(days=days))]
    if credential_id is not None:
        where.append("u.credential_id = ?")
        args.append(credential_id)
    if agent_id is not None:
        where.append("u.agent_id = ?")
        args.append(agent_id)
    rows = conn.execute(
        f"""SELECT substr(u.at, 1, 10) AS day, u.name, u.credential_id, u.agent_id, a.name AS agent_name,
                   COUNT(*) AS count, SUM(u.ok = 0) AS errors, MIN(u.at) AS first_at, MAX(u.at) AS last_at,
                   GROUP_CONCAT(DISTINCT u.tool) AS tools, GROUP_CONCAT(DISTINCT u.host) AS hosts,
                   (SELECT e.error FROM credential_uses e WHERE e.name = u.name AND e.agent_id IS u.agent_id
                      AND substr(e.at, 1, 10) = substr(u.at, 1, 10) AND e.ok = 0 ORDER BY e.id DESC LIMIT 1)
                     AS last_error
            FROM credential_uses u LEFT JOIN actors a ON a.id = u.agent_id WHERE {' AND '.join(where)}
            GROUP BY day, u.name, u.agent_id ORDER BY day DESC, errors > 0 DESC, last_at DESC
            LIMIT 200""", args).fetchall()
    return [{**dict(r), "errors": r["errors"] or 0, "tools": sorted(set((r["tools"] or "").split(",")) - {""}),
             "hosts": sorted(set((r["hosts"] or "").split(",")) - {""})} for r in rows]


def uses_filtered(conn: sqlite3.Connection, name: str | None = None, agent_id: int | None = None,
                  day: str | None = None, limit: int = 200) -> list[dict]:
    """The single uses behind one audit line (expand)."""
    from ..tasks import display_id

    store.ensure_schema(conn)
    where, args = [], []
    if name:
        where.append("u.name = ?")
        args.append(name)
    if agent_id is not None:
        where.append("u.agent_id = ?" if agent_id else "u.agent_id IS NULL")
        if agent_id:
            args.append(agent_id)
    if day:
        where.append("substr(u.at, 1, 10) = ?")
        args.append(day[:10])
    rows = conn.execute(
        f"""SELECT u.*, a.name AS agent_name FROM credential_uses u LEFT JOIN actors a ON a.id = u.agent_id
            {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY u.id DESC LIMIT ?""", (*args, limit)).fetchall()
    return [{**dict(r), "ok": bool(r["ok"]), "task_ref": display_id(r["task_id"]) if r["task_id"] else None}
            for r in rows]
