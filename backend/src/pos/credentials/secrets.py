"""Agent-managed secrets (Tajemství): agents create, share and rotate secrets without ever seeing a value.

docs/CREDENTIALS.md, "Agent secrets". A secret is one server-store entry (pos.credentials.serverstore: AES-GCM,
the key from POS_SECRETS_KEY) with named fields, plus one registry credential per field (pos.credentials.service):
`<name>` for the primary field and `<name>.<field>` for the others, each pointing at pos://<name>/<field>. From
there on it is a credential like any other: grants `cred:<name>`, `{{cred:<name>}}` in run_with_credentials and
credential_http, the allowed hosts and commands, the use log, the hourly limit, the redaction of every output and
the grounding check (they all read the registry).

A field's value comes from a source that never passes through the model:

- `generate`         random, made here (length, charset);
- `capture_browser`  a value the browser guard took off the agent's live page: browser_capture_secret(target=
                     'secret', ref=...) stages it here for 30 minutes and gives the agent a capture_id;
- `capture_command`  the stdout of one command in the agent's own sandbox (pos.sandbox), written to a file there,
                     read back here and deleted; never returned (only the exit code, and stderr when it fails);
- `from_secret`      copied or derived (base64, basic auth "user:password", hex, sha256) from a credential the agent
                     holds without a scope, or from an earlier field of the same secret (`self:<field>`);
- `value`            a literal the agent already knows from elsewhere: kept out of the audit, the run's transcript
                     and record (pos.runner._finish), but generate and capture are preferred.

`agent_secrets` (created on first use, no numbered migration) keeps the metadata, never a value: the creator, team,
project, purpose, hosts, sensitivity and how each field was made. The creator gets the grants. Sharing
(`secret_share`): inside the secret's team or project at once; across teams at once as well, audited
(`secret_share_cross_team`) and listed in the owner's daily access digest; a `high` secret (payment, banking, the
owner's own accounts, production DB write) only after the owner's one click in the approval queue (action
`secret.share`, business priority). Rotation keeps every grant. The owner's page Tajemství (/secrets and
/m/secrets, owner only) lists every secret, the operator's CLI entries (kniha-test-*) included, and never shows a
value: the platform has no endpoint that returns one (the owner reads values in 1Password or on the server).
"""

import base64
import hashlib
import json
import os
import re
import secrets as pysecrets
import sqlite3
import string
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import actors, audit
from ..core import Ctx, Forbidden, NotFound, now_iso
from . import onepassword, serverstore, service, store
from .redact import Redactor

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")  # kebab-case
NAME_MIN, NAME_MAX = 3, 48
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_FIELDS = 8
MAX_VALUE = 4096
MAX_TOTAL = 16_384
MAX_TEXT = 1000
GEN_MIN, GEN_MAX, GEN_DEFAULT = 12, 256, 32
CHARSETS = {
    "alnum": string.ascii_letters + string.digits,
    "hex": "0123456789abcdef",
    "base64url": string.ascii_letters + string.digits + "-_",
    "digits": string.digits,
    "letters": string.ascii_letters,
    "symbols": string.ascii_letters + string.digits + "!#%+-=?@^_~",
}
SOURCES = ("generate", "capture_browser", "capture_command", "from_secret", "value")
TRANSFORMS = ("copy", "base64", "basic_auth", "hex", "sha256")
SENSITIVITY = ("normal", "high")
DEFAULT_PER_AGENT = 20
COMMAND_TIMEOUT = 120
SHARE_ACTION = "secret.share"
# What makes a secret `high` by itself (the owner may lower it on the page): money, banking, the owner's own
# accounts, a production database. An agent may always say high itself.
HIGH_HINT = re.compile(
    r"(?<![a-z])(?:bank|banka|banky|banking|payment|payments|platb\w*|stripe|paypal|gopay|comgate|card|cards|"
    r"karta|karty|iban|revolut|fio|airbank|csob|moneta|owner|david|osobn\w*|personal)(?![a-z])"
    r"|prod(?:uction|ukcni)?[-_ .]?(?:db|database|postgres|mysql|sql)|(?:db|database|postgres|mysql)[-_ .]?prod",
    re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_secrets (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,            -- kebab-case; the server-store entry and the primary credential
    description   TEXT NOT NULL DEFAULT '',
    purpose       TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT 'agent',   -- agent | operator (the CLI's entries, shown on the page)
    created_by    INTEGER REFERENCES actors(id),   -- NULL: the operator
    team          TEXT,
    project_id    INTEGER,
    sensitivity   TEXT NOT NULL DEFAULT 'normal',  -- normal | high
    hosts         TEXT NOT NULL DEFAULT '[]',
    fields        TEXT NOT NULL DEFAULT '[]',      -- [{name, credential, source, spec}]: how each was made, no value
    primary_field TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    rotated_at    TEXT,
    deleted_at    TEXT
);
"""


class SecretError(ValueError):
    """A request that makes no sense (a bad name, a source, a limit); the message never holds a value."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    store.ensure_schema(conn)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'agent_secrets'").fetchone() is None:
        with conn:
            conn.executescript(SCHEMA)


def per_agent_limit() -> int:
    try:
        return max(1, int(os.environ.get("POS_SECRETS_MAX_PER_AGENT") or DEFAULT_PER_AGENT))
    except ValueError:
        return DEFAULT_PER_AGENT


def cred_name(secret: str, field_name: str, primary: str) -> str:
    """The registry credential of one field: the secret's own name for its primary field, else <name>.<field>."""
    return secret if field_name == primary else f"{secret}.{field_name}"


# ------------------------------------------------------------------ the run's values (redaction after the run)

# run_id -> [(credential name, value, literal)]: what this API process put into a secret during a run, so the run's
# transcript and record are redacted when the worker hands them in (pos.runner._finish). In memory only,
# for at most a day; the API is one process.
_RUN_VALUES: dict[int, list[tuple[str, str, bool]]] = {}
_RUN_AT: dict[int, float] = {}
_RUN_LOCK = threading.Lock()
RUN_KEEP_S = 24 * 3600


def _remember(run_id: int | None, name: str, value: str, literal: bool) -> None:
    if not run_id or not value:
        return
    with _RUN_LOCK:
        now = time.monotonic()
        for rid in [r for r, at in _RUN_AT.items() if now - at > RUN_KEEP_S]:
            _RUN_VALUES.pop(rid, None)
            _RUN_AT.pop(rid, None)
        _RUN_VALUES.setdefault(run_id, []).append((name, value, literal))
        _RUN_AT[run_id] = now


def scrub_run(conn: sqlite3.Connection, run_id: int, actor_id: int, *texts: str) -> list[str]:
    """The run's texts (the CLI's JSON lines, its detail) with every value it put into a secret redacted. A value
    that never passed through the model (generated, captured, derived) but shows up in the run anyway is a misuse
    signal (audit `secret_misuse`, pos.improve.signals)."""
    with _RUN_LOCK:
        items = _RUN_VALUES.pop(run_id, [])
        _RUN_AT.pop(run_id, None)
    if not items:
        return list(texts)
    red = Redactor({n: v for n, v, _ in items})
    out = [red(t or "") if t else (t or "") for t in texts]
    leaked = sorted({n for n, v, literal in items if not literal and any(v in (t or "") for t in texts)})
    if leaked:
        audit.log(conn, Ctx(actor_id, via="runner", run_id=run_id), "secret_misuse", "run", run_id,
                  kind="value_in_run", credentials=leaked)
    return out


# ------------------------------------------------------------------ captures staged by the browser guard

def stage_capture(agent_id: int, value: str, host: str, run_id: int | None) -> str:
    """browser_capture_secret(target='secret'): the value waits (encrypted, 30 min) for secret_create."""
    value = (value or "").strip()
    if not value or set(value) <= set("•*·●. "):
        raise SecretError("no value on the page there (masked or empty): reveal it first (reveal_ref)")
    if len(value) > MAX_VALUE or "\n" in value or "\r" in value:
        raise SecretError(f"a one-line value of at most {MAX_VALUE} characters")
    return serverstore.stage(str(agent_id), value, {"host": host, "run_id": run_id})


# ------------------------------------------------------------------ reading

def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["hosts"] = json.loads(d.get("hosts") or "[]")
    d["fields"] = json.loads(d.get("fields") or "[]")
    return d


def get(conn: sqlite3.Connection, name: str) -> dict:
    ensure_schema(conn)
    s = _row(conn.execute("SELECT * FROM agent_secrets WHERE name = ? AND deleted_at IS NULL",
                          (str(name or "").strip().lower(),)).fetchone())
    if s is None:
        raise NotFound(f"no secret called {name!r}: secret_list shows the ones you hold")
    return s


def credentials_of(conn: sqlite3.Connection, name: str) -> list[dict]:
    """The registry credentials that point into the server-store entry `name` (pos://name/...), not archived."""
    store.ensure_schema(conn)
    prefix = serverstore.ref(name, "")
    rows = conn.execute("SELECT * FROM credentials WHERE archived_at IS NULL AND substr(op_ref, 1, ?) = ? "
                        "ORDER BY name", (len(prefix), prefix)).fetchall()
    return [service._row(r) for r in rows]


def _caps(conn: sqlite3.Connection, s: dict) -> list[str]:
    return [service.capability(c["name"]) for c in credentials_of(conn, s["name"])]


def holders(conn: sqlite3.Connection, s: dict) -> dict[int, dict]:
    """{agent_id: {name, grants: [...]}} of the active grants on any of the secret's credentials."""
    out: dict[int, dict] = {}
    for c in credentials_of(conn, s["name"]):
        for g in service.grants(conn, name=c["name"]):
            e = out.setdefault(g["agent_id"], {"agent_id": g["agent_id"], "name": g["agent_name"], "grants": []})
            e["grants"].append({"id": g["id"], "credential": c["name"], "scope": g["scope"],
                                "granted_by": g["granted_by_name"], "source": g["source"],
                                "created_at": g["created_at"], "expires_at": g["expires_at"]})
    return out


def holds(conn: sqlite3.Connection, agent_id: int, s: dict) -> bool:
    return agent_id in holders(conn, s)


def _team(conn: sqlite3.Connection, actor_id: int | None) -> str:
    if not actor_id:
        return ""
    row = actors.get(conn, actor_id)
    return (row["team"] if "team" in row.keys() else None) or ""


def _in_project(conn: sqlite3.Connection, project_id: int | None, actor_id: int) -> bool:
    if not project_id:
        return False
    try:
        return conn.execute("SELECT 1 FROM project_members WHERE project_id = ? AND actor_id = ?",
                            (project_id, actor_id)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def same_team(conn: sqlite3.Connection, s: dict, agent_id: int) -> bool:
    """Inside the secret's team (the creator's team when it was made; no team counts as one team) or project."""
    return _team(conn, agent_id) == (s.get("team") or "") or _in_project(conn, s.get("project_id"), agent_id)


def _project_name(conn: sqlite3.Connection, project_id: int | None) -> str | None:
    if not project_id:
        return None
    r = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
    return r["name"] if r else None


def _use(conn: sqlite3.Connection, s: dict) -> dict:
    """How an agent uses it: the placeholders, the env var, the header, hosts, tools, commands."""
    creds = credentials_of(conn, s["name"])
    primary = next((c for c in creds if c["name"] == s["name"]), creds[0] if creds else None)
    return {"placeholders": [f"{{{{cred:{c['name']}}}}}" for c in creds],
            "env_var": (primary["env_var"] or service._default_env(primary["name"])) if primary else None,
            "header": primary["header"].split(":")[0] if primary and primary["header"] else None,
            "hosts": primary["allowed_hosts"] if primary else s["hosts"],
            "tools": (primary["allowed_tools"] or list(service.TOOLS)) if primary else [],
            "commands": primary["allowed_commands"] if primary else []}


def _brief(conn: sqlite3.Connection, s: dict, viewer: int | None = None) -> dict:
    who = holders(conn, s)
    creator = actors.get(conn, s["created_by"])["name"] if s["created_by"] else "operátor (CLI)"
    out = {"name": s["name"], "description": s["description"], "purpose": s["purpose"], "creator": creator,
           "team": s["team"] or None, "project": _project_name(conn, s["project_id"]),
           "sensitivity": s["sensitivity"], "hosts": s["hosts"],
           "fields": [{"name": f["name"], "credential": f.get("credential"), "source": f.get("source")}
                      for f in s["fields"]],
           "use": _use(conn, s), "holders": sorted(h["name"] for h in who.values()),
           "created_at": s["created_at"], "rotated_at": s["rotated_at"]}
    if viewer is not None:
        out["you_hold"] = viewer in who
        out["you_created"] = s["created_by"] == viewer
    return out


def agent_secret_of(conn: sqlite3.Connection, credential: dict) -> dict | None:
    """The agent's secret a registry credential belongs to (pos://<secret>/<field>), or None (1Password, the
    operator's entries). request_access refuses these: they are shared with secret_share only."""
    if not serverstore.is_ref(credential.get("op_ref")):
        return None
    ensure_schema(conn)
    try:
        entry = serverstore.parse_ref(credential["op_ref"])[0]
    except ValueError:
        return None
    s = _row(conn.execute("SELECT * FROM agent_secrets WHERE name = ? AND deleted_at IS NULL AND origin = 'agent'",
                          (entry,)).fetchone())
    if s is None:
        return None
    creator = actors.get(conn, s["created_by"])["name"] if s["created_by"] else "?"
    return {"name": s["name"], "creator": creator, "holders": sorted(h["name"] for h in holders(conn, s).values())}


def for_agent(conn: sqlite3.Connection, agent_id: int) -> list[dict]:
    """secret_list: the secrets an agent holds or created (names and metadata, never a value)."""
    adopt_operator_entries(conn)
    rows = [_row(r) for r in conn.execute("SELECT * FROM agent_secrets WHERE deleted_at IS NULL ORDER BY name")]
    out = []
    for s in rows:
        b = _brief(conn, s, agent_id)
        if b["you_hold"] or b["you_created"]:
            out.append(b)
    return out


# ------------------------------------------------------------------ validation

def _text(value, what: str, limit: int = MAX_TEXT) -> str:
    v = str(value or "").strip()
    if len(v) > limit:
        raise SecretError(f"{what}: at most {limit} characters")
    return v


def _check_name(conn: sqlite3.Connection, name: str) -> str:
    name = str(name or "").strip().lower()
    if not (NAME_MIN <= len(name) <= NAME_MAX) or not NAME_RE.match(name):
        raise SecretError(f"name: kebab-case, {NAME_MIN}-{NAME_MAX} characters, a-z 0-9 and single dashes "
                          "(like kniha-stripe-test)")
    if conn.execute("SELECT 1 FROM agent_secrets WHERE name = ? AND deleted_at IS NULL", (name,)).fetchone():
        raise SecretError(f"a secret called {name} exists already: secret_rotate it, or pick another name")
    if name in serverstore.names():
        raise SecretError(f"{name} is an entry of the operator's server store: pick another name")
    if conn.execute("SELECT 1 FROM credentials WHERE archived_at IS NULL AND (name = ? OR name GLOB ?)",
                    (name, name + ".*")).fetchone():
        raise SecretError(f"{name} is a credential in the registry already (credentials_list): pick another name")
    on, _ = onepassword.configured()
    if on:
        try:
            listed = onepassword.items()
        except onepassword.Unavailable:
            listed = []  # 1Password unreachable: the registry and the store above still keep names apart
        titles = {service._slug(i.get("title", "")) for i in listed}
        if name in titles:
            raise SecretError(f"{name} is the name of an item in 1Password: pick another name")
    return name


def _hosts(value) -> list[str]:
    try:
        return service._hosts(value or [])
    except service.CredentialError as e:
        raise SecretError(str(e)) from None


def _sensitivity(given: str | None, *texts: str) -> str:
    g = str(given or "").strip().lower()
    if g and g not in SENSITIVITY:
        raise SecretError(f"sensitivity: one of {SENSITIVITY}")
    if g == "high" or any(HIGH_HINT.search(t or "") for t in texts):
        return "high"
    return "normal"


def _project(conn: sqlite3.Connection, ref) -> int | None:
    if ref in (None, ""):
        return None
    ref = str(ref).strip().lstrip("#")
    row = conn.execute("SELECT id FROM projects WHERE id = ? OR slug = ?",
                       (int(ref) if ref.isdigit() else -1, ref)).fetchone()
    if row is None:
        raise SecretError(f"no project {ref!r} (project_list)")
    return row["id"]


def _spec(fname: str, raw, default) -> dict:
    """One field's source, normalized. Never stores a literal value in what it returns for metadata."""
    if raw is None or raw == "" or (isinstance(raw, dict) and not raw.get("source") and default is not None):
        raw = {**(default if isinstance(default, dict) else {"source": default or "generate"}),
               **(raw if isinstance(raw, dict) else {})}
    if isinstance(raw, str):
        raw = {"source": raw}
    if not isinstance(raw, dict):
        raise SecretError(f"field {fname}: a source object like {{'source': 'generate', 'length': 32}}")
    src = str(raw.get("source") or "").strip().lower()
    if src not in SOURCES:
        raise SecretError(f"field {fname}: source is one of {SOURCES}")
    spec: dict = {"source": src}
    if src == "generate":
        try:
            n = int(raw.get("length") or GEN_DEFAULT)
        except (TypeError, ValueError):
            raise SecretError(f"field {fname}: length is a number") from None
        if not GEN_MIN <= n <= GEN_MAX:
            raise SecretError(f"field {fname}: length {GEN_MIN} to {GEN_MAX}")
        cs = str(raw.get("charset") or "alnum").strip().lower()
        if cs not in CHARSETS:
            raise SecretError(f"field {fname}: charset is one of {tuple(CHARSETS)}")
        spec.update(length=n, charset=cs)
    elif src == "capture_browser":
        cid = str(raw.get("capture_id") or "").strip()
        if not serverstore.STAGE_RE.match(cid):
            raise SecretError(f"field {fname}: capture_id from browser_capture_secret(target='secret', ref=...) on "
                              "your live page (it stores the value for 30 minutes and gives you the id)")
        spec["capture_id"] = cid
    elif src == "capture_command":
        cmd = str(raw.get("command") or "").strip()
        if not cmd or len(cmd) > 4000:
            raise SecretError(f"field {fname}: command, one shell command (at most 4000 characters) whose stdout "
                              "is the value")
        try:
            t = int(raw.get("timeout") or COMMAND_TIMEOUT)
        except (TypeError, ValueError):
            raise SecretError(f"field {fname}: timeout is a number of seconds") from None
        spec.update(command=cmd, timeout=max(5, min(t, 600)))
    elif src == "from_secret":
        ref = str(raw.get("secret") or "").strip().lower()
        if not ref:
            raise SecretError(f"field {fname}: secret, a credential you hold (credentials_list) or self:<field>")
        tr = str(raw.get("transform") or "copy").strip().lower()
        if tr not in TRANSFORMS:
            raise SecretError(f"field {fname}: transform is one of {TRANSFORMS}")
        spec.update(secret=ref, transform=tr)
        if tr == "basic_auth":
            user, user_secret = raw.get("user"), str(raw.get("user_secret") or "").strip().lower()
            if not user and not user_secret:
                raise SecretError(f"field {fname}: basic_auth needs user (the user name) or user_secret")
            if user:
                spec["user"] = _text(user, f"field {fname}: user", 200)
            if user_secret:
                spec["user_secret"] = user_secret
    else:  # value
        v = raw.get("value")
        if not isinstance(v, str) or not v:
            raise SecretError(f"field {fname}: value, the literal (prefer generate or capture_*)")
        spec["value"] = v
    return spec


def _public(spec: dict) -> dict:
    """What the metadata keeps of a spec: everything but a literal value."""
    return {k: v for k, v in spec.items() if k != "value"}


def field_summary(fields, source=None) -> dict:
    """The fields and their sources for the audit of the call (never a value)."""
    out = {}
    items = fields.items() if isinstance(fields, dict) else ((f, None) for f in (fields or []))
    for k, v in items:
        if isinstance(v, dict):
            out[str(k)[:40]] = str(v.get("source") or (source.get("source") if isinstance(source, dict)
                                                       else source) or "?")[:20]
        else:
            out[str(k)[:40]] = str(v if isinstance(v, str) and v in SOURCES else
                                   (source.get("source") if isinstance(source, dict) else source) or "?")[:20]
    return out


def _held_credential(conn: sqlite3.Connection, ctx: Ctx, name: str) -> dict:
    """A credential the agent holds without a scope (a scoped grant must not be copied out of its scope)."""
    try:
        c = service.get(conn, name)
    except NotFound:
        _deny(conn, ctx, "from_secret", "no_such", name)
        raise SecretError(f"from_secret: no credential called {name!r} (credentials_list)") from None
    if c["archived_at"]:
        raise SecretError(f"from_secret: {name} is archived")
    if not any(g["scope"] is None for g in service.grants(conn, name=c["name"], agent_id=ctx.actor_id)):
        _deny(conn, ctx, "from_secret", "no_grant", name)
        raise Forbidden(f"from_secret: you do not hold cred:{name} (without a scope); request_access or ask its "
                        "holder to secret_share it")
    return c


def _deny(conn: sqlite3.Connection, ctx: Ctx, what: str, cause: str, name: str | None = None, **detail) -> None:
    """A refused secret operation: the audit trail and the improve loop's signal (secret:denied.<cause>)."""
    audit.log(conn, ctx, "secret_denied", None, None, what=what, cause=cause, secret=name, **detail)
    conn.commit()


# ------------------------------------------------------------------ creating, rotating

@dataclass
class Plan:
    """A validated create or rotate: everything checked, no value made yet (the command captures run between)."""
    name: str
    fields: list[tuple[str, dict]]                       # (field, spec) in the order they are made
    rotate: bool = False
    description: str = ""
    purpose: str = ""
    hosts: list[str] = field(default_factory=list)
    sensitivity: str = "normal"
    team: str = ""
    project_id: int | None = None
    primary: str = ""
    registry: list[dict] = field(default_factory=list)  # cleaned registry entries (create)
    task_id: int | None = None
    keep: dict[str, dict] = field(default_factory=dict)  # rotate: the fields not remade (their recorded spec)

    def commands(self) -> list[tuple[str, dict]]:
        return [(f, s) for f, s in self.fields if s["source"] == "capture_command"]


def _check_order(fields: list[tuple[str, dict]], existing: set[str]) -> None:
    seen = set(existing)
    for fname, spec in fields:
        for ref in (spec.get("secret"), spec.get("user_secret")):
            if ref and ref.startswith("self:"):
                other = ref[5:]
                if other == fname or other not in seen:
                    raise SecretError(f"field {fname}: {ref} names a field made before it in this secret")
        seen.add(fname)


def plan_create(conn: sqlite3.Connection, ctx: Ctx, *, name: str, description: str, fields, source=None,
                purpose: str = "", hosts=None, sensitivity: str | None = None, project=None,
                header: str | None = None, env_var: str | None = None, commands=None, tools=None,
                primary_field: str | None = None, task_id: int | None = None, max_uses_hour: int | None = None
                ) -> Plan:
    ensure_schema(conn)
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] == "human":
        raise Forbidden("secrets are made by agents (people keep theirs in 1Password)")
    try:
        name = _check_name(conn, name)
    except SecretError as e:
        _deny(conn, ctx, "create", "name", str(name or "")[:60])
        raise e from None
    mine = conn.execute("SELECT COUNT(*) FROM agent_secrets WHERE created_by = ? AND deleted_at IS NULL "
                        "AND origin = 'agent'", (ctx.actor_id,)).fetchone()[0]
    if mine >= per_agent_limit():
        _deny(conn, ctx, "create", "limit", name, count=mine)
        raise SecretError(f"you have {mine} secrets (limit {per_agent_limit()}): secret_delete ones you no longer "
                          "need")
    description = _text(description, "description")
    if not description:
        raise SecretError("description: what it is and what it opens (one or two sentences)")
    purpose = _text(purpose, "purpose")
    if isinstance(fields, dict):
        items = list(fields.items())
    elif isinstance(fields, list):
        items = [(f, None) for f in fields]
    else:
        raise SecretError("fields: {field: source} (like {'password': {'source': 'generate'}}) or a list of field "
                          "names with source")
    if not items or len(items) > MAX_FIELDS:
        raise SecretError(f"fields: 1 to {MAX_FIELDS}")
    specs: list[tuple[str, dict]] = []
    for k, raw in items:
        fname = str(k or "").strip().lower()
        if not FIELD_RE.match(fname):
            raise SecretError(f"field {fname!r}: a-z 0-9 _, starting with a letter, at most 32 characters")
        if any(fname == f for f, _ in specs):
            raise SecretError(f"field {fname}: given twice")
        specs.append((fname, _spec(fname, raw, source)))
    _check_order(specs, set())
    names = [f for f, _ in specs]
    primary = str(primary_field or "").strip().lower() or next(
        (p for p in ("password", "token", "value", "secret", "key") if p in names), names[0])
    if primary not in names:
        raise SecretError(f"primary_field: one of the fields ({', '.join(names)})")
    hosts_ = _hosts(hosts)
    cmds = commands if isinstance(commands, list) else ([commands] if commands else [])
    tool_list = tools if isinstance(tools, list) else ([tools] if tools else [])
    registry = []
    for fname, _ in specs:
        cname = cred_name(name, fname, primary)
        data = {"name": cname, "op_ref": serverstore.ref(name, fname),
                "description": f"Tajemství `{name}`" + (f" (pole {fname})" if fname != primary else "")
                + f" od {me['name']}: {description}"[:3900],
                "env_var": env_var if (fname == primary and env_var) else None,
                "header": header if fname == primary else None, "allowed_hosts": hosts_,
                "allowed_tools": tool_list, "allowed_commands": cmds, "max_uses_hour": max_uses_hour or 60,
                "notes": f"Vytvořil agent {me['name']} (secret_create)."}
        try:
            registry.append(service._clean(conn, data))
        except service.CredentialError as e:
            raise SecretError(str(e)) from None
    sens = _sensitivity(sensitivity, name, description, purpose, " ".join(hosts_))
    for _, spec in specs:  # a copy of a high secret is high
        if spec["source"] == "from_secret":
            for ref in (spec.get("secret"), spec.get("user_secret")):
                if ref and not ref.startswith("self:"):
                    c = _held_credential(conn, ctx, ref)
                    src = conn.execute("SELECT sensitivity FROM agent_secrets WHERE name = ? AND deleted_at IS NULL",
                                       (serverstore.parse_ref(c["op_ref"])[0] if serverstore.is_ref(c["op_ref"])
                                        else "",)).fetchone()
                    if src and src["sensitivity"] == "high":
                        sens = "high"
    return Plan(name=name, fields=specs, description=description, purpose=purpose, hosts=hosts_, sensitivity=sens,
                team=_team(conn, ctx.actor_id), project_id=_project(conn, project), primary=primary,
                registry=registry, task_id=task_id)


def _may_rotate(conn: sqlite3.Connection, ctx: Ctx, s: dict) -> None:
    if s["origin"] != "agent":
        _deny(conn, ctx, "rotate", "operator", s["name"])
        raise Forbidden(f"{s['name']} is the operator's (the CLI on the server): it is rotated there")
    if s["created_by"] == ctx.actor_id:
        return
    if holds(conn, ctx.actor_id, s) and same_team(conn, s, ctx.actor_id):
        return
    _deny(conn, ctx, "rotate", "not_allowed", s["name"])
    raise Forbidden(f"only {s['name']}'s creator or a holder in its team rotates it")


def plan_rotate(conn: sqlite3.Connection, ctx: Ctx, name: str, fields=None, source=None,
                task_id: int | None = None) -> Plan:
    """New values for some fields (default: every generated one, the same way); fields derived from a remade one
    (from_secret self:<field>) are derived again. The registry entries and every grant stay."""
    s = get(conn, name)
    _may_rotate(conn, ctx, s)
    recorded = {f["name"]: f for f in s["fields"]}
    if isinstance(fields, dict) and fields:
        wanted = list(fields.items())
    elif isinstance(fields, list) and fields:
        wanted = [(f, None) for f in fields]
    else:
        wanted = [(f["name"], None) for f in s["fields"] if (f.get("spec") or {}).get("source") == "generate"]
        if not wanted and source is None:
            raise SecretError(f"{s['name']} has no generated field: name the fields and their source")
    specs: dict[str, dict] = {}
    for k, raw in wanted:
        fname = str(k or "").strip().lower()
        if fname not in recorded:
            raise SecretError(f"{s['name']} has no field {fname!r} ({', '.join(recorded)})")
        if raw is None and source is None:
            old = dict(recorded[fname].get("spec") or {})
            if old.get("source") not in ("generate", "from_secret"):
                raise SecretError(f"field {fname} was made by {old.get('source')}: give its new source")
            raw = old
        specs[fname] = _spec(fname, raw, source)
    # Fields derived from a remade field follow it.
    changed = set(specs)
    for f in s["fields"]:
        sp = f.get("spec") or {}
        if f["name"] in specs or sp.get("source") != "from_secret":
            continue
        refs = {r[5:] for r in (sp.get("secret"), sp.get("user_secret")) if r and r.startswith("self:")}
        if refs & changed:
            specs[f["name"]] = dict(sp)
            changed.add(f["name"])
    ordered = [(f["name"], specs[f["name"]]) for f in s["fields"] if f["name"] in specs]
    _check_order(ordered, set(recorded) - set(specs))
    for _, spec in ordered:
        for ref in (spec.get("secret"), spec.get("user_secret")):
            if spec["source"] == "from_secret" and ref and not ref.startswith("self:"):
                _held_credential(conn, ctx, ref)
    return Plan(name=s["name"], fields=ordered, rotate=True, sensitivity=s["sensitivity"], primary=s["primary_field"],
                task_id=task_id, keep={f["name"]: f for f in s["fields"] if f["name"] not in specs})


async def run_commands(who: dict, plan: Plan) -> dict[str, str]:
    """capture_command: each command's stdout goes to a file in the agent's sandbox (not to the exec log), is read
    back and the file deleted. Nothing of it is returned; a failure says the exit code and stderr (redacted)."""
    from .. import sandbox

    out: dict[str, str] = {}
    for fname, spec in plan.commands():
        path = f"/var/tmp/.pos-secret-{pysecrets.token_hex(8)}"
        script = f"umask 077; ( {spec['command']}\n) > {path}"
        value = None
        try:
            res = await sandbox.run(who, script, spec["timeout"])
            try:
                data, _ = await sandbox.read(who, path, max_bytes=MAX_VALUE + 4)
                value = data.decode("utf-8", errors="replace").rstrip("\r\n")
            except sandbox.SandboxError:
                value = None
        except sandbox.SandboxError as e:
            raise SecretError(f"field {fname}: the sandbox: {str(e)[:200]}") from None
        finally:
            try:
                await sandbox.run(who, f"rm -f {path}", 30)
            except sandbox.SandboxError:
                pass
        red = Redactor({fname: value}) if value else Redactor()
        if res.get("exit_code") != 0:
            err = red(str(res.get("stderr") or ""))[-300:]
            raise SecretError(f"field {fname}: the command failed (exit {res.get('exit_code')})"
                              + (f": {err}" if err.strip() else ""))
        if not value:
            raise SecretError(f"field {fname}: the command printed nothing on stdout")
        if "\n" in value or "\r" in value:
            raise SecretError(f"field {fname}: the value has several lines; make it one (base64 -w0, tr -d '\\n')")
        if len(value) > MAX_VALUE:
            raise SecretError(f"field {fname}: over {MAX_VALUE} characters")
        out[fname] = value
    return out


def _derive(conn: sqlite3.Connection, ctx: Ctx, plan: Plan, fname: str, spec: dict, made: dict[str, str]) -> str:
    def source_value(ref: str) -> str:
        if ref.startswith("self:"):
            other = ref[5:]
            if other in made:
                return made[other]
            return serverstore.resolve(serverstore.ref(plan.name, other))  # rotate: a field kept as it is
        c = _held_credential(conn, ctx, ref)
        try:
            v = service.backend_resolve(c["op_ref"])
        except onepassword.Unavailable as e:
            service._log_use(conn, ctx, c, c["name"], ctx.actor_id, "derive", None, False, str(e)[:200],
                             ctx.run_id, plan.task_id)
            raise SecretError(f"field {fname}: {service.backend_label(c['op_ref'])} unavailable for {ref}") from None
        service._log_use(conn, ctx, c, c["name"], ctx.actor_id, "derive", None, True, None, ctx.run_id, plan.task_id)
        return v

    base = source_value(spec["secret"])
    tr = spec.get("transform") or "copy"
    if tr == "copy":
        return base
    if tr == "base64":
        return base64.b64encode(base.encode()).decode()
    if tr == "hex":
        return base.encode().hex()
    if tr == "sha256":
        return hashlib.sha256(base.encode()).hexdigest()
    user = spec.get("user") or source_value(spec["user_secret"])
    return base64.b64encode(f"{user}:{base}".encode()).decode()


def _make(conn: sqlite3.Connection, ctx: Ctx, plan: Plan, outputs: dict[str, str]) -> dict[str, str]:
    made: dict[str, str] = {}
    for fname, spec in plan.fields:
        src = spec["source"]
        if src == "generate":
            cs = CHARSETS[spec["charset"]]
            v = "".join(pysecrets.choice(cs) for _ in range(spec["length"]))
        elif src == "value":
            v = spec["value"]
        elif src == "capture_command":
            v = outputs.get(fname) or ""
        elif src == "capture_browser":
            try:
                v, _meta = serverstore.take_stage(spec["capture_id"], str(ctx.actor_id))
            except serverstore.Unavailable as e:
                raise SecretError(f"field {fname}: {e}") from None
        else:
            v = _derive(conn, ctx, plan, fname, spec, made)
        if not v or len(v) > MAX_VALUE or "\n" in v or "\r" in v:
            raise SecretError(f"field {fname}: a non-empty one-line value of at most {MAX_VALUE} characters")
        made[fname] = v
    if sum(len(v) for v in made.values()) > MAX_TOTAL:
        raise SecretError(f"the values together are over {MAX_TOTAL} characters")
    return made


def _insert_grants(conn: sqlite3.Connection, agent_id: int, granted_by: int, caps: list[str], source: str,
                   reason: str) -> list[int]:
    from ..access import service as access
    from ..access import store as astore

    astore.ensure_schema(conn)
    access._ensure_seeded(conn, agent_id)
    now = now_iso()
    out = []
    for cap in caps:
        if conn.execute(f"SELECT 1 FROM access_grants WHERE agent_id = ? AND capability = ? AND {astore.ACTIVE}",
                        (agent_id, cap, now)).fetchone():
            continue
        out.append(access._insert_grant(conn, agent_id, cap, granted_by, source, reason[:1000]))
    access.refresh_cache(conn, agent_id)
    return out


def finish(conn: sqlite3.Connection, ctx: Ctx, plan: Plan, outputs: dict[str, str] | None = None) -> dict:
    """Make the values, store them, register and grant (create) or replace them (rotate). Returns what the agent
    may know: names, fields and how to use it; never a value."""
    ensure_schema(conn)
    if not plan.rotate:  # the name may have been taken while a command ran
        if conn.execute("SELECT 1 FROM agent_secrets WHERE name = ? AND deleted_at IS NULL", (plan.name,)).fetchone():
            raise SecretError(f"a secret called {plan.name} exists already")
    on, why = serverstore.configured()
    if not on:
        raise SecretError(f"the server store is off ({why})")
    made = _make(conn, ctx, plan, outputs or {})
    me = actors.get(conn, ctx.actor_id)
    try:
        serverstore.put(plan.name, made, source=f"agent:{me['name']}", replace=not plan.rotate)
    except (ValueError, serverstore.Unavailable) as e:
        raise SecretError(f"not stored: {e}") from None
    for fname, spec in plan.fields:
        _remember(ctx.run_id, cred_name(plan.name, fname, plan.primary), made[fname], spec["source"] == "value")
    literal = sorted(f for f, s in plan.fields if s["source"] == "value")
    now = now_iso()
    if plan.rotate:
        s = get(conn, plan.name)
        recorded = {f["name"]: f for f in s["fields"]}
        for fname, spec in plan.fields:
            recorded[fname] = {**recorded[fname], "source": spec["source"], "spec": _public(spec)}
        conn.execute("UPDATE agent_secrets SET fields = ?, updated_at = ?, rotated_at = ? WHERE id = ?",
                     (json.dumps([recorded[f["name"]] for f in s["fields"]]), now, now, s["id"]))
        audit.log(conn, ctx, "secret_rotate", "secret", s["id"], secret=plan.name,
                  fields={f: sp["source"] for f, sp in plan.fields}, literal=literal or None)
        conn.commit()
        return {"name": plan.name, "rotated": [f for f, _ in plan.fields],
                "note": "New values stored; every holder keeps its grant and uses it by name as before. You never "
                        "see a value."}
    made.clear()
    try:
        for f in plan.registry:
            service._insert(conn, ctx, f)
        fields_meta = [{"name": fname, "credential": cred_name(plan.name, fname, plan.primary),
                        "source": spec["source"], "spec": _public(spec)} for fname, spec in plan.fields]
        sid = conn.execute(
            """INSERT INTO agent_secrets (name, description, purpose, origin, created_by, team, project_id, sensitivity,
                   hosts, fields, primary_field, created_at, updated_at)
               VALUES (?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan.name, plan.description, plan.purpose, ctx.actor_id, plan.team or None, plan.project_id,
             plan.sensitivity, json.dumps(plan.hosts), json.dumps(fields_meta), plan.primary, now, now)).lastrowid
        caps = [service.capability(f["name"]) for f in plan.registry]
        _insert_grants(conn, ctx.actor_id, ctx.actor_id, caps, "secret_create", f"vytvořil tajemství {plan.name}")
        audit.log(conn, ctx, "secret_create", "secret", sid, secret=plan.name, sensitivity=plan.sensitivity,
                  hosts=plan.hosts, fields={f: sp["source"] for f, sp in plan.fields}, literal=literal or None,
                  team=plan.team or None, project=plan.project_id)
        conn.commit()
    except Exception:
        conn.rollback()
        serverstore.delete(plan.name)  # nothing half made
        raise
    s = get(conn, plan.name)
    out = _brief(conn, s, ctx.actor_id)
    out["note"] = ("Stored; you never see the value. Use it by name: " + ", ".join(out["use"]["placeholders"])
                   + " in run_with_credentials / credential_http (within its hosts and commands). Share it with "
                   "secret_share, never in a message.")
    if literal:
        out["warning"] = ("A literal value was given: it is kept out of logs and the run's record, but prefer "
                          "generate or capture_* next time.")
    return out


# ------------------------------------------------------------------ sharing

def _targets(conn: sqlite3.Connection, to_agent: str | None, to_team: str | None) -> list[sqlite3.Row]:
    if bool(to_agent) == bool(to_team):
        raise SecretError("name one: to_agent (a member's name) or to_team (a team)")
    if to_agent:
        row = actors.find_by_name(conn, str(to_agent).strip())
        if row is None:
            raise NotFound(f"no member called {to_agent} (org_chart)")
        rows = [row]
    else:
        team = str(to_team).strip()
        rows = conn.execute("SELECT * FROM actors WHERE team = ? AND archived_at IS NULL AND kind != 'human' "
                            "ORDER BY name", (team,)).fetchall()
        if not rows:
            raise NotFound(f"no agents in the team {team!r}")
    for r in rows:
        if r["kind"] == "human" or r["archived_at"]:
            raise SecretError(f"{r['name']}: secrets are shared with active agents (people use 1Password)")
    return rows


def share(conn: sqlite3.Connection, ctx: Ctx, name: str, to_agent: str | None = None, to_team: str | None = None,
          reason: str = "", task_id: int | None = None) -> dict:
    """Give another agent (or a whole team) the grants. Same team or project: at once. Another team: at once,
    audited and in the owner's daily digest. A high secret: the owner's approval first (business priority)."""
    from .. import approvals
    from ..access import service as access

    s = get(conn, name)
    reason = _text(reason, "reason")
    if not reason:
        raise SecretError("reason: why the other agent needs it")
    if not holds(conn, ctx.actor_id, s):
        _deny(conn, ctx, "share", "not_holder", s["name"])
        raise Forbidden(f"you do not hold {s['name']}: only its holders share it")
    rows = [r for r in _targets(conn, to_agent, to_team) if r["id"] != ctx.actor_id]
    who = holders(conn, s)
    new = [r for r in rows if r["id"] not in who]
    if not new:
        return {"name": s["name"], "shared_with": [], "note": "they hold it already"}
    me = actors.get(conn, ctx.actor_id)
    caps = _caps(conn, s)
    cross = [r["name"] for r in new if not same_team(conn, s, r["id"])]
    if s["sensitivity"] == "high":
        names = ", ".join(r["name"] for r in new)
        details = {
            "kind": "secret", "priority": "business", "secret": s["name"], "sensitivity": "high",
            "to": [r["id"] for r in new], "to_names": [r["name"] for r in new], "from": ctx.actor_id,
            "from_name": me["name"], "cross_team": cross, "why": reason,
            "text": (f"**{me['name']}** chce sdílet citlivé tajemství `{s['name']}` ({s['description'][:300]}) "
                     f"s: **{names}**.\n\nDůvod: {reason}\n\nHodnotu neuvidí nikdo, ani agenti: schválením "
                     "dostanou jen právo ji použít (v rámci povolených hostů a příkazů)."
                     + (f"\n\nMimo tým tajemství: {', '.join(cross)}." if cross else "")),
        }
        a = approvals.request(conn, ctx, SHARE_ACTION, details, task_id=task_id)
        audit.log(conn, ctx, "secret_share_requested", "secret", s["id"], secret=s["name"], approval=a["id"],
                  to=[r["name"] for r in new], cross_team=cross or None, reason=reason)
        conn.commit()
        return {"name": s["name"], "pending_approval": a["id"], "to": [r["name"] for r in new],
                "note": f"{s['name']} is high sensitivity: the owner approves the share with one click (approval "
                        f"#{a['id']}); the grants are made then and you hear the decision in your inbox. Go on with "
                        "other work meanwhile."}
    shared = []
    for r in new:
        _insert_grants(conn, r["id"], ctx.actor_id, caps, "secret_share", f"{me['name']}: {reason}")
        kind = "secret_share" if r["name"] not in cross else "secret_share_cross_team"
        audit.log(conn, ctx, kind, "actor", r["id"], secret=s["name"], to=r["name"], from_=me["name"],
                  from_team=s["team"] or None, to_team=_team(conn, r["id"]) or None, reason=reason)
        access._tell(conn, ctx, r["id"], f"{me['name']} ti nasdílel tajemství `{s['name']}` ({s['description'][:200]})."
                                         f" Používej ho jménem: {{{{cred:{s['name']}}}}}. Důvod: {reason}",
                     notice=f"secret_share:{s['name']}")
        shared.append(r["name"])
    conn.commit()
    return {"name": s["name"], "shared_with": shared, "cross_team": cross,
            "note": "Shared: they use it by name; nobody sees the value." + (
                " Across teams: audited and in the owner's daily digest." if cross else "")}


def _on_approved(conn: sqlite3.Connection, a: dict) -> None:
    """The owner approved a high secret's share: the grants now (granted by him)."""
    if a.get("action") != SHARE_ACTION:
        return
    d = a.get("details") or {}
    try:
        s = get(conn, d.get("secret") or "")
    except NotFound:
        return
    owner = Ctx(a.get("decided_by") or actors.owner_id(conn), via="approval")
    caps = _caps(conn, s)
    for aid in d.get("to") or []:
        row = conn.execute("SELECT id, name, kind, archived_at FROM actors WHERE id = ?", (aid,)).fetchone()
        if row is None or row["kind"] == "human" or row["archived_at"]:
            continue
        _insert_grants(conn, aid, owner.actor_id, caps, "owner_approval",
                       f"schválené sdílení #{a['id']} ({d.get('from_name')}): {d.get('why') or ''}")
        cross = row["name"] in (d.get("cross_team") or [])
        audit.log(conn, owner, "secret_share_cross_team" if cross else "secret_share", "actor", aid,
                  secret=s["name"], to=row["name"], from_=d.get("from_name"), approval=a["id"],
                  reason=d.get("why"))


def revoke(conn: sqlite3.Connection, ctx: Ctx, name: str, from_agent: str | None = None, from_team: str | None = None,
           reason: str = "") -> dict:
    """End an agent's (or a team's) grants on a secret. The owner, the secret's creator, whoever shared it to
    that agent, and the agent itself may."""
    from ..access import service as access
    from ..access import store as astore

    s = get(conn, name)
    reason = _text(reason, "reason") or "odebráno"
    rows = _targets(conn, from_agent, from_team)
    me = actors.get(conn, ctx.actor_id)
    who = holders(conn, s)
    done = []
    for r in rows:
        held = who.get(r["id"])
        if not held:
            continue
        sharers = {g["granted_by"] for g in held["grants"]}
        if not (me["is_owner"] or s["created_by"] == ctx.actor_id or r["id"] == ctx.actor_id
                or me["name"] in sharers):
            _deny(conn, ctx, "revoke", "not_allowed", s["name"], target=r["name"])
            raise Forbidden(f"only the owner, {s['name']}'s creator, whoever shared it to {r['name']} or "
                            f"{r['name']} itself takes it away")
        ids = [g["id"] for g in held["grants"]]
        now = now_iso()
        for gid in ids:
            conn.execute("UPDATE access_grants SET ended_at = ?, ended_by = ?, end_kind = 'revoked', end_reason = ? "
                         f"WHERE id = ? AND {astore.ACTIVE}", (now, ctx.actor_id, reason[:500], gid, now))
        access.refresh_cache(conn, r["id"])
        audit.log(conn, ctx, "secret_revoke", "actor", r["id"], secret=s["name"], grants=ids, reason=reason)
        if r["id"] != ctx.actor_id:
            access._tell(conn, ctx, r["id"], f"{me['name']} ti odebral tajemství `{s['name']}`: {reason}",
                         notice=f"secret_revoke:{s['name']}")
        done.append(r["name"])
    conn.commit()
    return {"name": s["name"], "revoked": done}


def delete(conn: sqlite3.Connection, ctx: Ctx, name: str, reason: str = "") -> dict:
    """The secret goes: its value, its registry credentials and every grant. The owner, or its creator (an agent's
    own secret); the operator's entries only the owner."""
    s = get(conn, name)
    me = actors.get(conn, ctx.actor_id)
    if not (me["is_owner"] or (s["origin"] == "agent" and s["created_by"] == ctx.actor_id)):
        _deny(conn, ctx, "delete", "not_allowed", s["name"])
        raise Forbidden(f"only the owner or {s['name']}'s creator deletes it")
    reason = _text(reason, "reason") or "smazáno"
    now = now_iso()
    ended = []
    for c in credentials_of(conn, s["name"]):
        for g in service.grants(conn, name=c["name"]):
            conn.execute("UPDATE access_grants SET ended_at = ?, ended_by = ?, end_kind = 'revoked', end_reason = ? "
                         "WHERE id = ?", (now, ctx.actor_id, f"tajemství smazáno: {reason}"[:500], g["id"]))
            ended.append(g["id"])
        conn.execute("UPDATE credentials SET archived_at = ?, updated_at = ? WHERE id = ?", (now, now, c["id"]))
    from ..access import service as access

    agents_ = {r[0] for r in conn.execute(
        f"SELECT agent_id FROM access_grants WHERE id IN ({','.join('?' * len(ended))})", ended)} if ended else set()
    for aid in agents_:
        access.refresh_cache(conn, aid)
    conn.execute("UPDATE agent_secrets SET deleted_at = ?, updated_at = ?, name = name || '~' || id WHERE id = ?",
                 (now, now, s["id"]))
    serverstore.delete(s["name"])
    audit.log(conn, ctx, "secret_delete", "secret", s["id"], secret=s["name"], grants=ended, reason=reason)
    conn.commit()
    return {"deleted": s["name"], "grants_ended": len(ended)}


# ------------------------------------------------------------------ the owner's page (Tajemství)

def _require_owner(conn: sqlite3.Connection, ctx: Ctx) -> None:
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Forbidden("Tajemství are the owner's page")


def adopt_operator_entries(conn: sqlite3.Connection) -> int:
    """The operator's CLI entries (kniha-test-*) on the page: one row each (origin operator), metadata only."""
    ensure_schema(conn)
    known = {r[0] for r in conn.execute("SELECT name FROM agent_secrets WHERE deleted_at IS NULL")}
    n = 0
    for m in serverstore.entries():
        if m["name"] in known:
            continue
        creds = credentials_of(conn, m["name"])
        hosts = sorted({h for c in creds for h in c["allowed_hosts"]})
        fields = [{"name": f, "credential": next((c["name"] for c in creds if c["op_ref"] == serverstore.ref(
            m["name"], f)), None), "source": "operator"} for f in m["fields"]]
        primary = next((f for f in ("authorization", "password") if f in m["fields"]), m["fields"][0] if m["fields"]
                       else "")
        now = now_iso()
        conn.execute(
            """INSERT INTO agent_secrets (name, description, purpose, origin, created_by, team, sensitivity, hosts,
                   fields, primary_field, created_at, updated_at)
               VALUES (?, ?, '', 'operator', NULL, NULL, ?, ?, ?, ?, ?, ?)""",
            (m["name"], f"Položka operátora na serveru ({m['source'] or 'CLI'})",
             _sensitivity(None, m["name"]), json.dumps(hosts), json.dumps(fields), primary,
             m.get("created_at") or now, m.get("updated_at") or now))
        n += 1
    if n:
        conn.commit()
    return n


def overview(conn: sqlite3.Connection, ctx: Ctx) -> dict:
    """Every secret with its creator, holders, last use and sensitivity; pending shares. Never a value."""
    from .. import approvals

    _require_owner(conn, ctx)
    adopt_operator_entries(conn)
    rows = [_row(r) for r in conn.execute("SELECT * FROM agent_secrets WHERE deleted_at IS NULL ORDER BY name")]
    pending: dict[str, list[dict]] = {}
    for a in approvals.pending(conn):
        if a["action"] == SHARE_ACTION:
            d = a["details"]
            pending.setdefault(d.get("secret") or "", []).append(
                {"approval_id": a["id"], "from": d.get("from_name"), "to": d.get("to_names") or [],
                 "why": d.get("why"), "at": a["created_at"]})
    out = []
    for s in rows:
        creds = credentials_of(conn, s["name"])
        ids = [c["id"] for c in creds]
        last = None
        uses_7d = 0
        if ids:
            q = ",".join("?" * len(ids))
            r = conn.execute(f"""SELECT u.at, u.ok, u.tool, a.name AS agent FROM credential_uses u
                                 LEFT JOIN actors a ON a.id = u.agent_id WHERE u.credential_id IN ({q})
                                 AND u.tool != 'test' ORDER BY u.id DESC LIMIT 1""", ids).fetchone()
            last = dict(r) if r else None
            if last:
                last["ok"] = bool(last["ok"])
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
            uses_7d = conn.execute(f"SELECT COUNT(*) FROM credential_uses WHERE credential_id IN ({q}) AND at > ? "
                                   "AND tool != 'test'", (*ids, since)).fetchone()[0]
        who = holders(conn, s)
        out.append({
            "name": s["name"], "description": s["description"], "purpose": s["purpose"], "origin": s["origin"],
            "creator": actors.get(conn, s["created_by"])["name"] if s["created_by"] else None,
            "creator_id": s["created_by"], "team": s["team"], "project": _project_name(conn, s["project_id"]),
            "sensitivity": s["sensitivity"], "hosts": s["hosts"],
            "fields": [{"name": f["name"], "credential": f.get("credential"), "source": f.get("source")}
                       for f in s["fields"]],
            "credentials": [c["name"] for c in creds],
            "holders": sorted(({"agent_id": h["agent_id"], "name": h["name"],
                                "granted_by": sorted({g["granted_by"] or "?" for g in h["grants"]}),
                                "since": min(g["created_at"] for g in h["grants"])} for h in who.values()),
                              key=lambda h: h["name"]),
            "last_use": last, "uses_7d": uses_7d, "pending": pending.get(s["name"], []),
            "created_at": s["created_at"], "rotated_at": s["rotated_at"], "stored": s["name"] in serverstore.names(),
        })
    on, why = serverstore.configured()
    return {"secrets": out, "store": {"enabled": on, "reason": why or None}, "per_agent_limit": per_agent_limit(),
            "values_shown": False}


def set_sensitivity(conn: sqlite3.Connection, ctx: Ctx, name: str, sensitivity: str) -> dict:
    _require_owner(conn, ctx)
    s = get(conn, name)
    v = str(sensitivity or "").strip().lower()
    if v not in SENSITIVITY:
        raise SecretError(f"sensitivity: one of {SENSITIVITY}")
    conn.execute("UPDATE agent_secrets SET sensitivity = ?, updated_at = ? WHERE id = ?", (v, now_iso(), s["id"]))
    audit.log(conn, ctx, "secret_sensitivity", "secret", s["id"], secret=s["name"], before=s["sensitivity"], after=v)
    conn.commit()
    return {"name": s["name"], "sensitivity": v}


def owner_revoke(conn: sqlite3.Connection, ctx: Ctx, name: str, agent_id: int, reason: str) -> dict:
    _require_owner(conn, ctx)
    row = actors.get(conn, agent_id)
    return revoke(conn, ctx, name, from_agent=row["name"], reason=reason or "odebral majitel")


def digest_lines(conn: sqlite3.Connection, since: str, limit: int = 8) -> list[str]:
    """Cross-team shares since `since`, for the owner's daily access digest (pos.access.service.digest)."""
    rows = conn.execute("""SELECT l.detail, a.name AS actor FROM audit_log l LEFT JOIN actors a ON a.id = l.actor_id
                           WHERE l.action = 'secret_share_cross_team' AND l.at > ? ORDER BY l.id""",
                        (since,)).fetchall()
    out = []
    for r in rows[:limit]:
        d = json.loads(r["detail"] or "{}")
        out.append(f"- `{d.get('secret')}`: {d.get('from_') or r['actor']} ({d.get('from_team') or 'bez týmu'}) → "
                   f"{d.get('to')} ({d.get('to_team') or 'bez týmu'})"
                   + (f" — {str(d.get('reason'))[:120]}" if d.get("reason") else ""))
    if len(rows) > limit:
        out.append(f"- … a {len(rows) - limit} dalších (stránka Tajemství)")
    return out


def _register_hooks() -> None:
    from .. import approvals

    if _on_approved not in approvals._on_approved:
        approvals.on_approved(_on_approved)


_register_hooks()
