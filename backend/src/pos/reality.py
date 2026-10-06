"""What is actually usable now ("Co je živé"): a machine-checkable registry per project.

The owner, 2026-10-06: marketing prepared e-mails saying "you can try it here, here's the link" while
nothing that could be tried existed; tasks were reported done with their parts dropped; agents told him
"DNS is missing" when it was not. Claims were not grounded in reality. This registry is the reality.

Each **capability** (or user-journey step) of a project has:

- `status`: live | unverified | test_only | mock | missing. **Only a verification makes it live**: a
  platform probe (an anonymous HTTP fetch run by PersonalOS, never the agent's own word) or a reviewer
  accepting submitted evidence (`verify`: the owner or an agent with tasks:review that did not submit it).
  An agent may declare test_only, mock, missing or unverified, never live.
- `url`, `access` (public | password | internal), the probe (`probe_mode` live: a passing probe makes
  it live; protected: it must NOT be reachable anonymously, a test instance open to the world is
  flagged; test: it runs on a password-protected test instance, see below), `aliases` (the words that
  name it in a text: the claim gate and the sequencing use them).
- **On a test instance** (`probe_mode` test): the platform fetches `probe_url` with a registered
  credential (`probe_credential`, e.g. kniha-test-basic-auth; the value is put into that one request
  by pos.credentials.platform_header and never logged) and anonymously. The authenticated fetch must
  pass (2xx, `probe_expect` on the page), and when `deploy_ref` is set the version deployed there (the
  deployer's status file, POS_DEPLOY_STATUS) must contain that commit. Then the capability is
  **test_only and verified** (`verified` in the view; it expires like live). It never becomes live: that
  still takes the anonymous probe of a public URL. An anonymous fetch that gets in is flagged as exposed.
- `verified_at` + `evidence`: a live capability whose last passing verification is older than
  EXPIRY_HOURS (72) reads as **unverified** (`effective`), and the probe job writes that down; a failing
  probe reverts it at once.

Agents read it with `reality_list`; the owner sees it on the project page ("Co je živé"). The claim gate
(pos.grounding) checks outbound and owner-facing content against it; the sequencing below holds
promotion tasks until what they promote is live.

**Sequencing**: an agent's marketing / outreach / content task (by the creator's or assignee's role, its
topic or title) in a project with a registry that names a capability that is not live is created as
`waiting` with a note and a row in `reality_holds`. It cannot be claimed or reopened by an agent while
held; the probe job (`tick`, every 30 min) and every verification release it once all its capabilities
are live, and wake the assignee. The owner moving it releases it (his escape hatch).

Tables are created on first use (no numbered migration, like pos.project_info).
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import actors, audit
from .core import Ctx, Forbidden, NotFound, now_iso

STATUSES = ("live", "unverified", "test_only", "mock", "missing")
AGENT_STATUSES = ("unverified", "test_only", "mock", "missing")  # live only through a verification
ACCESS = ("public", "password", "internal")
PROBE_MODES = ("live", "protected", "test")
EXPIRY_HOURS = float(os.environ.get("POS_REALITY_EXPIRY_HOURS") or 72)
NETWORK_ENV = "POS_GROUNDING_NETWORK"  # 0: no DNS or HTTP from the registry and the gates (tests)
FETCH_TIMEOUT_S = 6.0
BODY_LIMIT = 300_000
UA = "PersonalOS reality probe (anonymous outsider)"
# Where the deployers write what runs on a test instance (pos.reality.deployed_contains): the Kniha deployer's
# status.txt in the Kniha agents' checkout (mounted read-only into the API as /agent-work).
DEPLOY_STATUS_ENV = "POS_DEPLOY_STATUS"
DEPLOY_STATUS_DEFAULT = "/agent-work/kniha/.deploy/status.txt"
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

STATUS_CS = {"live": "živé (ověřeno)", "unverified": "neověřeno", "test_only": "jen testovací",
             "mock": "atrapa (mock)", "missing": "chybí"}
ACCESS_CS = {"public": "veřejné", "password": "na heslo", "internal": "interní"}

# Who promotes: these roles' tasks (created or assigned) are marketing / outreach / content.
PROMO_ROLES = {"growth", "growth_sales", "content", "marketing_lead", "marketing", "community", "sales"}
PROMO_TOPICS = {"marketing", "outreach", "content", "obsah", "kampan", "kampaň", "newsletter", "linkedin",
                "socials", "social", "pr", "growth", "prodej", "sales", "propagace", "reklama"}
PROMO_TITLE_RE = re.compile(r"\b(?:kampa[nň]|newsletter|outreach|osloven|oslovit|propagac|propagovat|marketing|"
                            r"reklam|příspěv|prispev|linkedin|post\b|announce|oznámen|oznamen|e-?mail(?:ing)?\s+"
                            r"(?:pro|partner|zákazník|zakaznik)|mailing|launch|spuštění kampaně|landing)",
                            re.IGNORECASE)

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reality_capabilities (
        id               INTEGER PRIMARY KEY,
        project_id       INTEGER NOT NULL REFERENCES projects(id),
        key              TEXT NOT NULL,
        name             TEXT NOT NULL,
        kind             TEXT NOT NULL DEFAULT 'capability',
        position         INTEGER NOT NULL DEFAULT 0,
        status           TEXT NOT NULL DEFAULT 'unverified',
        url              TEXT,
        access           TEXT NOT NULL DEFAULT 'public',
        aliases          TEXT NOT NULL DEFAULT '[]',
        probe_mode       TEXT,
        probe_url        TEXT,
        probe_expect     TEXT,
        notes            TEXT NOT NULL DEFAULT '',
        verified_at      TEXT,
        verified_by      TEXT,
        verify_method    TEXT,
        evidence         TEXT,
        last_check_at    TEXT,
        last_check_ok    INTEGER,
        last_check_detail TEXT,
        created_by       INTEGER,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        archived_at      TEXT,
        UNIQUE (project_id, key)
    )""",
    """CREATE TABLE IF NOT EXISTS reality_evidence (
        id            INTEGER PRIMARY KEY,
        capability_id INTEGER NOT NULL REFERENCES reality_capabilities(id),
        submitted_by  INTEGER NOT NULL,
        evidence      TEXT NOT NULL,
        url           TEXT,
        status        TEXT NOT NULL DEFAULT 'pending',
        decided_by    INTEGER,
        decided_at    TEXT,
        note          TEXT,
        created_at    TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reality_holds (
        task_id      INTEGER PRIMARY KEY REFERENCES tasks(id),
        project_id   INTEGER NOT NULL,
        capabilities TEXT NOT NULL DEFAULT '[]',
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        released_at  TEXT,
        released_by  TEXT
    )""",
]


_COLUMNS = {"probe_credential": "TEXT", "deploy_ref": "TEXT"}  # added after the first release


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA:
        conn.execute(sql)
    have = {r[1] for r in conn.execute("PRAGMA table_info(reality_capabilities)")}
    for col, typ in _COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE reality_capabilities ADD COLUMN {col} {typ}")


def network_on() -> bool:
    return os.environ.get(NETWORK_ENV, "1") != "0"


def norm(text: str | None) -> str:
    """Lower case without accents, for matching words in Czech text."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return plain.lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(iso: str | None, now: datetime | None = None) -> float:
    if not iso:
        return 1e9
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return ((now or _now()) - t).total_seconds() / 3600


# ------------------------------------------------------------------ the anonymous outsider probe

@dataclass
class Fetched:
    status: int
    final_url: str
    headers: dict = field(default_factory=dict)
    body: str = ""


@dataclass
class Probe:
    url: str
    kind: str          # ok | password | login | http | unreachable | dns | private | content | bad | unverifiable
    why: str = ""      # Czech, for the agent and the owner
    status: int | None = None
    final_url: str = ""
    addresses: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def verified(self) -> bool:
        """The probe actually ran (not switched off, not a bad address)."""
        return self.kind != "unverifiable"

    def as_dict(self) -> dict:
        return {"url": self.url, "kind": self.kind, "why": self.why, "status": self.status,
                "final_url": self.final_url}


def _default_resolve(host: str) -> list[str]:
    return sorted({i[4][0] for i in socket.getaddrinfo(host, None)})


def _default_fetch(url: str, timeout: float, headers: dict | None = None) -> Fetched:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=5,
                      headers={"User-Agent": UA, "Accept": "text/html,*/*", **(headers or {})}) as c:
        with c.stream("GET", url) as r:
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > BODY_LIMIT:
                    break
            body = b"".join(chunks).decode(errors="ignore")
            return Fetched(r.status_code, str(r.url), {k.lower(): v for k, v in r.headers.items()}, body)


# Tests replace these (no network in the suite).
RESOLVE = _default_resolve
FETCH = _default_fetch

LOGIN_RE = re.compile(r"(?:^|[/._-])(?:login|log-in|signin|sign-in|sso|auth|prihlaseni|prihlasit|accounts\.google)"
                      r"(?:[/._?-]|$)", re.IGNORECASE)
PASSWORD_FIELD_RE = re.compile(r"""<input[^>]+type\s*=\s*["']?password""", re.IGNORECASE)


def resolve(host: str) -> list[str]:
    try:
        return RESOLVE(host)
    except (OSError, UnicodeError):
        return []


def probe(url: str, expect_text: str | None = None, timeout: float = FETCH_TIMEOUT_S,
          headers: dict | None = None) -> Probe:
    """Fetch `url` the way an outsider would: no cookie, no password, from the public internet. ok only
    for a 2xx page that asks for no password and no login (and shows `expect_text` when given).
    `headers`: the test-instance probe's credential (pos.credentials.platform_header); never logged."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return Probe(url, "bad", "není to webová adresa (http/https)")
    if not network_on():
        return Probe(url, "unverifiable", "síťové kontroly jsou vypnuté")
    host = parts.hostname.lower()
    addrs = resolve(host)
    if not addrs:
        return Probe(url, "dns", f"doména {host} se nepřekládá (DNS)")
    try:
        if not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs):
            return Probe(url, "private", f"{host} je interní adresa (není z internetu)", addresses=addrs)
    except ValueError:
        pass
    try:
        f = FETCH(url, timeout, headers=headers) if headers else FETCH(url, timeout)
    except Exception as e:  # noqa: BLE001 - any network failure: the outsider cannot open it
        return Probe(url, "unreachable", f"{host} neodpovídá ({type(e).__name__})", addresses=addrs)
    p = Probe(url, "ok", f"HTTP {f.status}", status=f.status, final_url=f.final_url, addresses=addrs)
    final = urlsplit(f.final_url or url)
    if f.status == 401 or "www-authenticate" in f.headers:
        p.kind, p.why = "password", f"odkaz {host} vyžaduje heslo (HTTP {f.status})"
    elif f.final_url and (final.hostname or "") + final.path != (parts.hostname or "") + parts.path \
            and LOGIN_RE.search((final.hostname or "") + final.path) and not LOGIN_RE.search(parts.path):
        p.kind, p.why = "login", f"odkaz {host} přesměruje na přihlášení ({final.hostname}{final.path})"
    elif not 200 <= f.status < 300:
        p.kind, p.why = "http", f"odkaz {url} nefunguje (HTTP {f.status})"
    elif PASSWORD_FIELD_RE.search(f.body or ""):
        p.kind, p.why = "password", f"stránka {host} chce heslo (formulář s heslem)"
    elif expect_text and norm(expect_text) not in norm(f.body):
        p.kind, p.why = "content", f"stránka {url} neobsahuje „{expect_text}“"
    return p


# ------------------------------------------------------------------ the registry

def _project_row(conn: sqlite3.Connection, ref) -> sqlite3.Row:
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM projects WHERE lower(slug) = lower(?)", (str(ref).lstrip("#"),)).fetchone()
    if row is None:
        raise NotFound(f"project {ref}")
    return row


def effective(row, now: datetime | None = None) -> str:
    """The status as it holds now: a live capability without a passing verification in EXPIRY_HOURS
    is unverified."""
    st = row["status"]
    if st == "live" and _age_hours(row["verified_at"], now) > EXPIRY_HOURS:
        return "unverified"
    return st


def verified(row, now: datetime | None = None) -> bool:
    """Verified now: live (fresh), or test_only with a passing test-instance probe in EXPIRY_HOURS."""
    st = effective(row, now)
    if st == "live":
        return True
    return st == "test_only" and bool(row["verified_at"]) and _age_hours(row["verified_at"], now) <= EXPIRY_HOURS


def view(row, now: datetime | None = None) -> dict:
    st = effective(row, now)
    keys = row.keys()
    out = {k: row[k] for k in ("id", "key", "name", "kind", "position", "url", "access", "probe_mode", "probe_url",
                               "probe_expect", "notes", "verified_at", "verified_by", "verify_method", "evidence",
                               "last_check_at", "last_check_detail")}
    out.update({k: (row[k] if k in keys else None) for k in _COLUMNS})
    out["verified"] = verified(row, now)
    out.update({"project_id": row["project_id"], "status": st, "declared": row["status"],
                "status_cs": STATUS_CS[st], "access_cs": ACCESS_CS.get(row["access"], row["access"]),
                "aliases": json.loads(row["aliases"] or "[]"), "live": st == "live",
                "last_check_ok": None if row["last_check_ok"] is None else bool(row["last_check_ok"]),
                "expired": row["status"] == "live" and st != "live"})
    return out


def rows(conn: sqlite3.Connection, project_ids) -> list[sqlite3.Row]:
    ensure_schema(conn)
    ids = [int(i) for i in (project_ids if isinstance(project_ids, (list, tuple, set)) else [project_ids]) if i]
    if not ids:
        return []
    return conn.execute(f"SELECT * FROM reality_capabilities WHERE archived_at IS NULL AND project_id IN "
                        f"({','.join('?' * len(ids))}) ORDER BY project_id, position, id", ids).fetchall()


def registry(conn: sqlite3.Connection, project_ids) -> list[dict]:
    now = _now()
    return [view(r, now) for r in rows(conn, project_ids)]


def has_registry(conn: sqlite3.Connection, project_id: int | None) -> bool:
    return bool(project_id) and bool(rows(conn, [project_id]))


def get(conn: sqlite3.Connection, project_id: int, key: str) -> sqlite3.Row:
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM reality_capabilities WHERE project_id = ? AND key = ? AND archived_at IS NULL",
                     (project_id, key)).fetchone()
    if r is None:
        raise NotFound(f"capability {key!r} in project {project_id}")
    return r


def fingerprint(caps: list[dict]) -> str:
    """Changes when the registry's truth changes (for caching the LLM verdict)."""
    raw = json.dumps([[c["key"], c["status"], c.get("url"), c.get("access"), c.get("notes")] for c in caps],
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _key(text: str) -> str:
    k = re.sub(r"[^a-z0-9]+", "_", norm(text)).strip("_")[:40]
    if not k:
        raise _invalid("key: a short name of the capability (letters, digits)")
    return k


def _invalid(msg: str):
    from .tasks import Invalid

    return Invalid(msg)


def _is_person(conn: sqlite3.Connection, actor_id: int) -> bool:
    r = conn.execute("SELECT kind FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return r is not None and r["kind"] == "human"


def upsert(conn: sqlite3.Connection, ctx: Ctx, project, key: str, fields: dict) -> dict:
    """Add or change a capability. Never makes it live: that takes a verification (`verify`, the probe).
    Changing the URL, access or probe of a live capability makes it unverified again."""
    ensure_schema(conn)
    p = _project_row(conn, project)
    key = _key(key)
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    unknown = set(fields) - {"name", "kind", "position", "status", "url", "access", "aliases", "probe_mode",
                             "probe_url", "probe_expect", "notes", *_COLUMNS}
    if unknown:
        raise _invalid(f"unknown fields {sorted(unknown)}")
    if fields.get("status") == "live":
        raise _invalid("status live is set only by a verification: the platform probe (probe_mode=live with a "
                       "public url) or a reviewer accepting evidence (reality_submit_evidence, then reality_verify)")
    if "status" in fields and fields["status"] not in AGENT_STATUSES:
        raise _invalid(f"status is one of {AGENT_STATUSES}")
    if "access" in fields and fields["access"] not in ACCESS:
        raise _invalid(f"access is one of {ACCESS}")
    if fields.get("probe_mode") not in (None, "", *PROBE_MODES):
        raise _invalid(f"probe_mode is one of {PROBE_MODES} (or empty)")
    for u in ("url", "probe_url"):
        if fields.get(u) and urlsplit(str(fields[u])).scheme not in ("http", "https"):
            raise _invalid(f"{u} is an http(s) address")
    if fields.get("deploy_ref"):
        fields["deploy_ref"] = str(fields["deploy_ref"]).strip().lower()
        if not SHA_RE.match(fields["deploy_ref"]):
            raise _invalid("deploy_ref is a commit sha (7-40 hex characters) the deployed version must contain")
    if fields.get("probe_credential"):
        fields["probe_credential"] = _probe_credential(conn, ctx, str(fields["probe_credential"]))
    if "aliases" in fields:
        al = fields["aliases"]
        al = [a for a in (al.split(",") if isinstance(al, str) else list(al))]
        fields["aliases"] = json.dumps([str(a).strip() for a in al if str(a).strip()][:30], ensure_ascii=False)
    now = now_iso()
    prev = conn.execute("SELECT * FROM reality_capabilities WHERE project_id = ? AND key = ?", (p["id"], key)).fetchone()
    if prev is None:
        if not fields.get("name"):
            raise _invalid("a new capability needs a name (Czech, what the user can do)")
        vals = {"project_id": p["id"], "key": key, "status": "unverified", "access": "public", "aliases": "[]",
                "created_by": ctx.actor_id, "created_at": now, "updated_at": now, **fields}
        cols = ", ".join(vals)
        conn.execute(f"INSERT INTO reality_capabilities ({cols}) VALUES ({', '.join('?' * len(vals))})",
                     list(vals.values()))
        audit.log(conn, ctx, "reality_add", "project", p["id"], key=key, status=vals["status"], url=vals.get("url"))
    else:
        changes = dict(fields)
        reset = prev["status"] == "live" and any(
            k in changes and changes[k] != prev[k] for k in ("url", "access", "probe_url", "probe_expect"))
        moved = any(k in changes and changes[k] != prev[k]
                    for k in ("url", "access", "probe_url", "probe_expect", "probe_mode", *_COLUMNS))
        if reset:
            changes.update({"status": "unverified", "verified_at": None, "verified_by": None, "verify_method": None})
        elif moved and prev["verify_method"] == TEST_METHOD:  # what the test probe verified is not this any more
            changes.update({"verified_at": None, "verified_by": None, "verify_method": None})
        changes.update({"updated_at": now, "archived_at": None})
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE reality_capabilities SET {sets} WHERE id = ?", [*changes.values(), prev["id"]])
        audit.log(conn, ctx, "reality_update", "project", p["id"], key=key, fields=sorted(fields),
                  **({"unverified": True} if reset else {}))
    out = view(get(conn, p["id"], key))
    release_holds(conn, p["id"])
    return out


def _probe_credential(conn: sqlite3.Connection, ctx: Ctx, name: str) -> str:
    """A credential the test-instance probe may use: registered, and the one who names it a person or an agent
    holding it (nobody points the platform's probe at a credential they could not use themselves)."""
    from .credentials import service as creds

    name = name.strip().lower()
    try:
        c = creds.get(conn, name)
    except NotFound:
        raise _invalid(f"probe_credential: no credential {name!r} in the registry (credentials_list)") from None
    if c["archived_at"]:
        raise _invalid(f"probe_credential: {name} is archived")
    if not _is_person(conn, ctx.actor_id) and not creds.grants(conn, name=name, agent_id=ctx.actor_id):
        raise Forbidden(f"probe_credential: you do not hold cred:{name}")
    return name


def archive(conn: sqlite3.Connection, ctx: Ctx, project, key: str) -> None:
    p = _project_row(conn, project)
    row = get(conn, p["id"], key)
    conn.execute("UPDATE reality_capabilities SET archived_at = ?, updated_at = ? WHERE id = ?",
                 (now_iso(), now_iso(), row["id"]))
    audit.log(conn, ctx, "reality_archive", "project", p["id"], key=key)


def _set_live(conn: sqlite3.Connection, row, method: str, by: str, evidence: str) -> None:
    now = now_iso()
    conn.execute("""UPDATE reality_capabilities SET status = 'live', verified_at = ?, verified_by = ?,
                    verify_method = ?, evidence = ?, updated_at = ? WHERE id = ?""",
                 (now, by, method, evidence[:2000], now, row["id"]))


def submit_evidence(conn: sqlite3.Connection, ctx: Ctx, project, key: str, evidence: str, url: str = "") -> dict:
    """An agent shows that a capability works (what it did, what it saw; the URL a user opens). A reviewer
    decides (`verify`); the submitter never makes its own claim live."""
    ensure_schema(conn)
    p = _project_row(conn, project)
    row = get(conn, p["id"], _key(key))
    evidence = (evidence or "").strip()
    if len(evidence) < 20:
        raise _invalid("evidence: what you checked as a real user and what it showed (a sentence at least)")
    cur = conn.execute("""INSERT INTO reality_evidence (capability_id, submitted_by, evidence, url, created_at)
                          VALUES (?, ?, ?, ?, ?)""", (row["id"], ctx.actor_id, evidence[:4000], url or row["url"],
                                                      now_iso()))
    audit.log(conn, ctx, "reality_evidence", "project", p["id"], key=row["key"], evidence_id=cur.lastrowid)
    return {"evidence_id": cur.lastrowid, "key": row["key"], "status": "pending",
            "note": "a reviewer (the project lead, QA or the owner) checks it with reality_verify; until then "
                    "the capability is not live"}


def _may_verify(conn: sqlite3.Connection, ctx: Ctx) -> None:
    me = actors.get(conn, ctx.actor_id)
    if me["is_owner"] or me["kind"] == "human":
        return
    from . import agents

    if not agents.has_permission(conn, ctx.actor_id, "tasks:review"):
        raise Forbidden("verifying a capability needs tasks:review (a reviewer, the project lead or the owner)")


def verify(conn: sqlite3.Connection, ctx: Ctx, project, key: str, accept: bool, note: str = "",
           evidence_id: int | None = None) -> dict:
    """A reviewer accepts (or rejects) the evidence that a capability works. Accepting a capability with a
    public URL also needs that URL to pass the outsider probe now: a reviewer cannot make a broken link live."""
    ensure_schema(conn)
    _may_verify(conn, ctx)
    p = _project_row(conn, project)
    row = get(conn, p["id"], _key(key))
    ev = None
    if evidence_id:
        ev = conn.execute("SELECT * FROM reality_evidence WHERE id = ? AND capability_id = ?",
                          (evidence_id, row["id"])).fetchone()
    else:
        ev = conn.execute("SELECT * FROM reality_evidence WHERE capability_id = ? AND status = 'pending' "
                          "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    owner = actors.get(conn, ctx.actor_id)["is_owner"]
    if ev is None and not owner:
        raise _invalid("no evidence waits for this capability: the team submits it with reality_submit_evidence")
    if ev is not None and ev["submitted_by"] == ctx.actor_id and not owner:
        raise Forbidden("nobody verifies their own evidence; another reviewer decides")
    who = actors.get(conn, ctx.actor_id)["name"]
    if accept:
        if row["access"] != "public":
            raise _invalid(f"{row['name']} is {ACCESS_CS.get(row['access'])}: only a public capability is live for "
                           "a real user. Make it public first (and change access), or keep it test_only")
        url = (ev["url"] if ev is not None and ev["url"] else None) or row["probe_url"] or row["url"]
        if url:
            pr = probe(url, row["probe_expect"])
            _record_check(conn, row, pr)
            if pr.verified and not pr.ok:
                raise _invalid(f"not accepted: {pr.why}. A capability a user cannot open is not live")
        _set_live(conn, row, "review", who, (ev["evidence"] if ev is not None else note) or note)
    if ev is not None:
        conn.execute("UPDATE reality_evidence SET status = ?, decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
                     ("accepted" if accept else "rejected", ctx.actor_id, now_iso(), note[:1000], ev["id"]))
    audit.log(conn, ctx, "reality_verify", "project", p["id"], key=row["key"], accept=accept, note=note[:300])
    release_holds(conn, p["id"])
    return view(get(conn, p["id"], row["key"]))


def _record_check(conn: sqlite3.Connection, row, pr: Probe) -> None:
    if not pr.verified:
        return
    conn.execute("UPDATE reality_capabilities SET last_check_at = ?, last_check_ok = ?, last_check_detail = ? "
                 "WHERE id = ?", (now_iso(), int(pr.ok), pr.why[:500], row["id"]))


TEST_METHOD = "probe_test"


def _status_files() -> list[str]:
    raw = os.environ.get(DEPLOY_STATUS_ENV)
    return [p.strip() for p in (DEPLOY_STATUS_DEFAULT if raw is None else raw).split(",") if p.strip()]


def _deployed_sha(host: str) -> tuple[str | None, str | None]:
    """The commit running on `host` per a deployer's status file (a section naming https://<host>, its
    "live:" line), and that checkout's folder."""
    from pathlib import Path

    for f in _status_files():
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section = False
        for line in text.splitlines():
            if line and not line[0].isspace():
                section = f"//{host}" in line.lower()
            elif section and line.strip().startswith("live:"):
                sha = line.split(":", 1)[1].strip().split()[0] if line.split(":", 1)[1].strip() else ""
                if SHA_RE.match(sha.lower()):
                    return sha.lower(), str(Path(f).resolve().parent.parent)
    return None, None


def _default_deployed_contains(host: str, ref: str) -> tuple[bool, str]:
    """Does the version deployed on `host` contain commit `ref`? (ok, why in Czech)."""
    import subprocess

    sha, repo = _deployed_sha(host)
    if not sha:
        return False, f"nevím, která verze běží na {host} (stav nasazení není k dispozici)"
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", repo, "merge-base", "--is-ancestor", ref, sha],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False, "verzi nasazení nejde ověřit (git)"
    if r.returncode == 0:
        return True, f"nasazená verze {sha[:7]} obsahuje {ref[:7]}"
    if r.returncode == 1:
        return False, f"na {host} běží {sha[:7]}, ta {ref[:7]} ještě neobsahuje (nenasazeno)"
    return False, f"commit {ref[:7]} nebo {sha[:7]} v repozitáři není"


DEPLOYED_CONTAINS = _default_deployed_contains  # tests replace it


def _probe_test(conn: sqlite3.Connection, row, url: str, ctx: Ctx, out: dict) -> dict:
    """A capability on a password-protected test instance: the authenticated probe (and the deployed version)
    make it test_only and verified; an anonymous fetch that gets in flags the instance as exposed."""
    from .credentials import service as creds

    host = _host(url)
    anon = probe(url)
    if not anon.verified:
        return {**out, **anon.as_dict()}
    exposed = anon.ok
    if exposed and not _recent(conn, "reality_exposed", row["project_id"], row["key"], hours=24):
        audit.log(conn, ctx, "reality_exposed", "project", row["project_id"], key=row["key"], url=url)
    cred = row["probe_credential"]
    ok, why, auth = False, "", None
    if not cred:
        why = "chybí probe_credential: testovací instanci nejde ověřit bez hesla"
    else:
        try:
            name, value = creds.platform_header(conn, ctx, cred, host, f"reality probe {row['key']}")
            auth = probe(url, row["probe_expect"], headers={name: value})
            del value
        except creds.CredentialError as e:
            why = f"heslo {cred} pro ověření nejde použít ({str(e)[:200]})"
    if auth is not None:
        if not auth.verified:
            return {**out, **auth.as_dict()}
        ok = auth.ok
        why = f"s heslem: HTTP {auth.status}" if ok else f"s heslem: {auth.why}"
    if ok and row["deploy_ref"]:
        ok, dwhy = DEPLOYED_CONTAINS(host, row["deploy_ref"])
        why = f"{why}; {dwhy}"
    if exposed:
        why = f"{why}; POZOR: {url} je dostupné i bez hesla"
    now = now_iso()
    if ok:
        conn.execute("""UPDATE reality_capabilities SET status = CASE WHEN status = 'live' THEN status ELSE 'test_only'
                        END, verified_at = ?, verified_by = 'probe', verify_method = ?, evidence = ?, updated_at = ?,
                        last_check_at = ?, last_check_ok = 1, last_check_detail = ? WHERE id = ?""",
                     (now, TEST_METHOD, f"testovací instance {url} ({why}, {now[:16]})"[:2000], now, now, why[:500],
                      row["id"]))
        if not (row["status"] == "test_only" and row["verify_method"] == TEST_METHOD and row["verified_at"]):
            audit.log(conn, ctx, "reality_test_verified", "project", row["project_id"], key=row["key"], url=url)
    else:
        conn.execute("UPDATE reality_capabilities SET last_check_at = ?, last_check_ok = 0, last_check_detail = ? "
                     "WHERE id = ?", (now, why[:500], row["id"]))
        if row["verify_method"] == TEST_METHOD and row["verified_at"]:
            conn.execute("UPDATE reality_capabilities SET verified_at = NULL, updated_at = ? WHERE id = ?",
                         (now, row["id"]))
            audit.log(conn, ctx, "reality_test_failed", "project", row["project_id"], key=row["key"], why=why[:300])
    return {**out, "kind": "ok" if ok else "failed", "why": why, "verified": ok, "exposed": exposed}


def run_probe(conn: sqlite3.Connection, row, ctx: Ctx | None = None) -> dict:
    """One capability's probe and what follows from it."""
    url = row["probe_url"] or row["url"]
    mode = row["probe_mode"]
    out = {"key": row["key"], "mode": mode, "url": url}
    if not url or mode not in PROBE_MODES:
        return {**out, "skipped": True}
    if mode == "test":
        return _probe_test(conn, row, url, ctx or _system(conn), out)
    pr = probe(url, row["probe_expect"] if mode == "live" else None)
    out.update(pr.as_dict())
    if not pr.verified:
        return out
    ctx = ctx or _system(conn)
    if mode == "live":
        _record_check(conn, row, pr)
        if pr.ok and row["access"] == "public":
            _set_live(conn, row, "probe", "probe", f"HTTP {pr.status} {pr.final_url or url} ({now_iso()[:16]})")
            if row["status"] != "live":
                audit.log(conn, ctx, "reality_live", "project", row["project_id"], key=row["key"], url=url)
            out["live"] = True
        elif not pr.ok and row["status"] == "live":
            conn.execute("UPDATE reality_capabilities SET status = 'unverified', updated_at = ? WHERE id = ?",
                         (now_iso(), row["id"]))
            audit.log(conn, ctx, "reality_unverified", "project", row["project_id"], key=row["key"], why=pr.why)
            out["live"] = False
    else:  # protected: an outsider must not get in
        exposed = pr.ok
        why = (f"{url} je dostupné bez hesla: testovací instance je otevřená světu" if exposed
               else f"chráněno ({pr.why})")
        conn.execute("UPDATE reality_capabilities SET last_check_at = ?, last_check_ok = ?, last_check_detail = ? "
                     "WHERE id = ?", (now_iso(), int(not exposed), why[:500], row["id"]))
        if exposed and not _recent(conn, "reality_exposed", row["project_id"], row["key"], hours=24):
            audit.log(conn, ctx, "reality_exposed", "project", row["project_id"], key=row["key"], url=url)
        out["exposed"] = exposed
    return out


def _recent(conn: sqlite3.Connection, action: str, project_id: int, key: str, hours: float) -> bool:
    since = (_now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return conn.execute("SELECT 1 FROM audit_log WHERE action = ? AND entity = 'project' AND entity_id = ? "
                        "AND at >= ? AND detail LIKE ? LIMIT 1",
                        (action, project_id, since, f'%"key": "{key}"%')).fetchone() is not None


def expire(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Live capabilities whose verification is older than EXPIRY_HOURS become unverified (written down)."""
    ensure_schema(conn)
    out = []
    for r in conn.execute("SELECT * FROM reality_capabilities WHERE status = 'live' AND archived_at IS NULL").fetchall():
        if effective(r, now) != "live":
            conn.execute("UPDATE reality_capabilities SET status = 'unverified', updated_at = ? WHERE id = ?",
                         (now_iso(), r["id"]))
            audit.log(conn, _system(conn), "reality_expired", "project", r["project_id"], key=r["key"],
                      verified_at=r["verified_at"])
            out.append(r["key"])
    return out


def _system(conn: sqlite3.Connection) -> Ctx:
    from . import business

    return business.system_ctx(conn)


def probe_project(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    return [run_probe(conn, r) for r in rows(conn, [project_id])]


def tick(conn: sqlite3.Connection) -> dict:
    """The scheduler's job: every probe, the expiry, then the holds whose capabilities went live."""
    ensure_schema(conn)
    seed_kniha(conn)
    results = [run_probe(conn, r) for r in conn.execute(
        "SELECT * FROM reality_capabilities WHERE archived_at IS NULL AND probe_mode IS NOT NULL").fetchall()]
    expired = expire(conn)
    released = release_holds(conn)
    conn.commit()
    return {"probed": len([r for r in results if not r.get("skipped")]),
            "live": sum(1 for r in results if r.get("live")), "expired": expired, "released": released}


# ------------------------------------------------------------------ matching text to capabilities

def _alias_patterns(cap: dict) -> list[re.Pattern]:
    words = [cap["name"], *cap.get("aliases", [])]
    out = []
    for w in words:
        n = norm(w).strip()
        if len(n) >= 3:
            out.append(re.compile(r"(?<![a-z0-9])" + re.escape(n)))
    return out


def _host(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def mentioned(text: str, caps: list[dict]) -> list[dict]:
    """Capabilities a text names (by name, alias, or a link to their host)."""
    t = norm(text)
    hosts = {_host(u) for u in re.findall(r"https?://[^\s<>()\"']+", text or "")}
    hosts |= {h for h in re.findall(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b", t)}
    out = []
    for c in caps:
        h = _host(c.get("url"))
        if (h and h in hosts) or any(p.search(t) for p in _alias_patterns(c)):
            out.append(c)
    return out


def caps_for_url(caps: list[dict], url: str) -> list[dict]:
    """Capabilities whose URL is this link (same host, the link under its path)."""
    h, path = _host(url), urlsplit(url).path or "/"
    out = []
    for c in caps:
        for u in (c.get("url"), c.get("probe_url")):
            if u and _host(u) == h and path.startswith(urlsplit(u).path.rstrip("/") or "/"):
                out.append(c)
                break
    return out


# ------------------------------------------------------------------ sequencing: promotion waits for live

def _role(conn: sqlite3.Connection, actor_id: int | None) -> str:
    if not actor_id:
        return ""
    r = conn.execute("SELECT role, kind FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return (r["role"] or "") if r is not None and r["kind"] != "human" else ""


def is_promotion(conn: sqlite3.Connection, task: dict) -> bool:
    if (task.get("topic") or "").lower() in PROMO_TOPICS:
        return True
    if _role(conn, task.get("created_by")) in PROMO_ROLES or _role(conn, task.get("assignee_id")) in PROMO_ROLES:
        return True
    return bool(PROMO_TITLE_RE.search(task.get("title") or ""))


def held(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    ensure_schema(conn)
    return conn.execute("SELECT * FROM reality_holds WHERE task_id = ? AND released_at IS NULL", (task_id,)).fetchone()


def hold_note(caps: list[dict]) -> str:
    names = ", ".join(f"„{c['name']}“ ({STATUS_CS[c['status']]})" for c in caps)
    return (f"Čeká na živé funkce: {names}. Propagovat lze jen to, co je v „Co je živé“ ověřené; úkol se sám "
            "vrátí do fronty, až budou živé (reality_list).")


def on_task_created(conn: sqlite3.Connection, ctx: Ctx, task: dict) -> dict | None:
    """An agent's promotion task that names a capability that is not live waits for it. Never raises."""
    try:
        if _is_person(conn, ctx.actor_id) or task.get("status") in ("done", "waiting"):
            return None
        pid = task.get("project_id")
        if not pid:
            return None
        caps = registry(conn, [pid])
        if not caps:
            return None
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        t = dict(row)
        if not is_promotion(conn, t):
            return None
        text = " ".join(str(t.get(k) or "") for k in ("title", "notes", "definition_of_done"))
        blocked = [c for c in mentioned(text, caps) if c["status"] != "live"]
        if not blocked:
            return None
        from . import comments, tasks, versioning

        note = hold_note(blocked)
        conn.execute("""INSERT OR REPLACE INTO reality_holds (task_id, project_id, capabilities, note, created_at)
                        VALUES (?, ?, ?, ?, ?)""", (t["id"], pid, json.dumps([c["key"] for c in blocked]), note,
                                                     now_iso()))
        versioning.update(conn, _system(conn), tasks.ENTITY, t["id"], {"status": "waiting", "progress_note": note[:500]},
                          action="reality_hold")
        comments.log(conn, _system(conn), t["id"], f"PersonalOS: {note}", "system")
        audit.log(conn, ctx, "reality_hold", "task", t["id"], capabilities=[c["key"] for c in blocked])
        return {"held": True, "capabilities": [c["key"] for c in blocked], "note": note}
    except Exception:  # noqa: BLE001 - sequencing must never fail creating the task
        import logging

        logging.getLogger(__name__).exception("reality hold failed for task %s", task.get("id"))
        return None


def refuse_if_held(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> None:
    """An agent may not start a held promotion task; a person moving it releases the hold."""
    h = held(conn, task_id)
    if h is None:
        return
    if _is_person(conn, ctx.actor_id):
        release(conn, task_id, f"owner:{ctx.actor_id}", wake_it=False)
        return
    caps = {c["key"]: c for c in registry(conn, [h["project_id"]])}
    waiting = [caps[k] for k in json.loads(h["capabilities"]) if k in caps and caps[k]["status"] != "live"]
    if not waiting:
        release(conn, task_id, "live", wake_it=False)
        return
    raise _invalid(f"{hold_note(waiting)} Do something else now; nothing about these may go out yet.")


def release(conn: sqlite3.Connection, task_id: int, by: str, wake_it: bool = True) -> None:
    conn.execute("UPDATE reality_holds SET released_at = ?, released_by = ? WHERE task_id = ? AND released_at IS NULL",
                 (now_iso(), by, task_id))
    if by != "live":
        audit.log(conn, _system(conn), "reality_release", "task", task_id, by=by)
        return
    from . import comments, tasks, versioning, wake

    t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is not None and t["status"] == "waiting":
        versioning.update(conn, _system(conn), tasks.ENTITY, task_id, {
            "status": "next", "progress_note": "Funkce, na které úkol čekal, jsou živé a ověřené: pokračuj."},
            action="reality_release")
        comments.log(conn, _system(conn), task_id, "PersonalOS: funkce jsou živé a ověřené; úkol je zpět ve frontě.",
                     "system")
        if wake_it and t["assignee_id"]:
            wake.wake(t["assignee_id"])
    audit.log(conn, _system(conn), "reality_release", "task", task_id, by=by)


def release_holds(conn: sqlite3.Connection, project_id: int | None = None) -> list[int]:
    """Holds whose every capability is live now are released (the task back to `next`, the agent woken)."""
    ensure_schema(conn)
    q = "SELECT * FROM reality_holds WHERE released_at IS NULL" + (" AND project_id = ?" if project_id else "")
    out = []
    for h in conn.execute(q, (project_id,) if project_id else ()).fetchall():
        caps = {c["key"]: c for c in registry(conn, [h["project_id"]])}
        keys = json.loads(h["capabilities"] or "[]")
        if all(k in caps and caps[k]["status"] == "live" for k in keys):
            release(conn, h["task_id"], "live")
            out.append(h["task_id"])
    return out


# ------------------------------------------------------------------ the Kniha seed

KNIHA_TEST = "https://kniha-test.obseum.cz"
KNIHA_TEST_CREDENTIAL = "kniha-test-basic-auth"
# The test-instance probe (probe_mode test): which credential, the page it opens, the commit it must run.
KNIHA_TEST_PROBES = {
    "order": {"probe_url": f"{KNIHA_TEST}/objednat", "deploy_ref": "a8f4f15"},  # T-883 the order form
    "photos": {"probe_url": f"{KNIHA_TEST}/objednat", "deploy_ref": "cfd3f24"},  # T-884 photos in the portal
    "payment": {"probe_url": f"{KNIHA_TEST}/objednat", "deploy_ref": "2015d8d"},  # T-888 transfer + QR Platba
}
# Seeded as missing on 2026-10-06 before they were on the test instance: still in that state, they are moved.
KNIHA_UPGRADE_FROM = {"order": "missing", "photos": "missing", "payment": "missing"}

KNIHA = [
    # key, name, status, url, access, probe_mode, probe_expect, aliases, notes
    ("landing", "Úvodní stránka Rodinné příběhy", "unverified", "https://rodinne-pribehy.obseum.cz", "public", "live",
     "Rodinné příběhy", ["landing", "úvodní stránka", "webová stránka", "rodinne-pribehy.obseum.cz"],
     "Veřejný web s popisem služby. Živé, když ho probe otevře bez hesla."),
    ("reservation", "Rezervační formulář (nezávazná rezervace)", "unverified", "https://rodinne-pribehy.obseum.cz",
     "public", "live", "rezerv", ["rezervac", "rezervovat", "zarezervujte", "zarezervovat", "nezávazn"],
     "Rezervace na úvodní stránce; ukládá se do reservations.jsonl (kniha_reservations_summary)."),
    ("test_app", "Testovací aplikace (kniha-test)", "test_only", "https://kniha-test.obseum.cz", "password",
     "protected", None, ["kniha-test", "aplikac", "appk", "testovací verz", "testovací aplikac"],
     "Jen pro tým: testovací instance, nikdy ji neposílej zákazníkům ani partnerům."),
    ("narrator", "Hlasový vypravěč (nahrávání vyprávění)", "mock", None, "internal", None, None,
     ["vypravěč", "nahrávání", "nahrajte", "nahrát", "hlasov", "mikrofon"],
     "Ve vývoji: rozhovor s vypravěčem běží jen jako atrapa (mock)."),
    ("chapters", "Kapitoly knihy z vyprávění", "mock", None, "internal", None, None,
     ["kapitol", "generování knihy", "složení knihy", "text knihy"], "Skládání kapitol je zatím atrapa (mock)."),
    ("order", "Objednávkový formulář", "test_only", f"{KNIHA_TEST}/objednat", "password", "test", "Objednat knihu",
     ["objedn", "koupit", "kupte", "nákup"],
     "Jen na testovací instanci (za heslem): veřejně zatím ne, dokud platba a e-maily nejsou ostré."),
    ("photos", "Nahrávání fotek", "test_only", KNIHA_TEST, "password", "test", "Objednat knihu",
     ["fotk", "fotograf", "foto"],
     "Jen na testovací instanci: fotky v rodinném portálu (/o/<token>/fotky, až 80, v náhledu i v PDF). Probe "
     "ověří, že instance běží a nasazená verze je obsahuje (T-884)."),
    ("payment", "Platba převodem s QR Platbou", "test_only", KNIHA_TEST, "password", "test", "QR kód",
     ["platb", "zaplat", "plaťte", "platební", "kartou", "qr platb", "qr kód"],
     "Jen na testovací instanci: převod s QR Platbou (SPD) a ruční potvrzení v adminu (T-888); online "
     "platební brána není. Ověřeno, až ji formulář nabízí (QR kód): bez čísla účtu a ceny "
     "(PLATBA_UCET, PLATBA_CASTKA_KC) je vypnutá a objednávka nezávazná."),
    ("email", "E-maily zákazníkům (potvrzení, kniha@obseum.cz)", "missing", None, "public", None, None,
     ["kniha@obseum.cz", "potvrzovací e-mail", "potvrzení e-mailem"], "Schránka kniha@obseum.cz zatím neexistuje."),
]


def _kniha_project(conn: sqlite3.Connection) -> sqlite3.Row | None:
    for q, arg in (("SELECT * FROM projects WHERE lower(slug) = ?", "kniha"),
                   ("SELECT * FROM projects WHERE lower(slug) IN ('rodinne-pribehy', 'kniha-rodinne-pribehy')", None),
                   ("SELECT * FROM projects WHERE lower(name) LIKE ? ORDER BY id LIMIT 1", "%kniha%")):
        r = conn.execute(q, (arg,) if arg is not None else ()).fetchone()
        if r is not None:
            return r
    return None


def seed_kniha(conn: sqlite3.Connection) -> int:
    """The Kniha registry as audited on 2026-10-06; idempotent: only capabilities not there yet are added
    (never live: the probe makes the landing page and the reservation live)."""
    ensure_schema(conn)
    p = _kniha_project(conn)
    if p is None:
        return 0
    n, moved = 0, []
    now = now_iso()
    for pos, (key, name, status, url, access, mode, expect, aliases, notes) in enumerate(KNIHA):
        extra = KNIHA_TEST_PROBES.get(key, {})
        cred = KNIHA_TEST_CREDENTIAL if mode == "test" else None
        probe_url = extra.get("probe_url") or (url if mode else None)
        prev = conn.execute("SELECT * FROM reality_capabilities WHERE project_id = ? AND key = ?",
                            (p["id"], key)).fetchone()
        if prev is not None:
            # still exactly as an older seed left it (nobody changed it since): brought up to date
            if (KNIHA_UPGRADE_FROM.get(key) == prev["status"] and not prev["url"] and not prev["probe_mode"]
                    and prev["status"] != status):
                conn.execute("""UPDATE reality_capabilities SET name = ?, status = ?, url = ?, access = ?, aliases = ?,
                                probe_mode = ?, probe_url = ?, probe_expect = ?, notes = ?, probe_credential = ?,
                                deploy_ref = ?, updated_at = ? WHERE id = ?""",
                             (name, status, url, access, json.dumps(aliases, ensure_ascii=False), mode, probe_url,
                              expect, notes, cred, extra.get("deploy_ref"), now, prev["id"]))
                moved.append(key)
            continue
        conn.execute("""INSERT INTO reality_capabilities (project_id, key, name, kind, position, status, url, access,
                        aliases, probe_mode, probe_url, probe_expect, notes, probe_credential, deploy_ref, created_at,
                        updated_at) VALUES (?, ?, ?, 'journey_step', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (p["id"], key, name, pos, status, url, access, json.dumps(aliases, ensure_ascii=False), mode,
                      probe_url, expect, notes, cred, extra.get("deploy_ref"), now, now))
        n += 1
    if n or moved:
        audit.log(conn, _system(conn), "reality_seed", "project", p["id"], added=n, **({"moved": moved} if moved else {}))
    return n + len(moved)


# ------------------------------------------------------------------ MCP

def register_mcp(mcp, session) -> None:
    from typing import Any

    from mcp.server.mcpserver import Context

    from . import mcp_server

    mcp_server.TOOL_PERMISSIONS.setdefault("reality_list", "tasks:read")
    mcp_server.TOOL_PERMISSIONS.setdefault("reality_upsert", "tasks:claim")
    mcp_server.TOOL_PERMISSIONS.setdefault("reality_submit_evidence", "tasks:claim")
    mcp_server.TOOL_PERMISSIONS.setdefault("reality_verify", "tasks:review")

    @mcp.tool(name="reality_list", description=(
        "What is actually usable now in a project ('Co je živé'): each capability or user-journey step with its "
        "status (live = verified working for a real user; unverified, test_only, mock, missing), URL, access "
        "(public | password | internal) and when it was last verified. Read it before you write, link or promise "
        "anything to customers, partners or the owner: only `live` exists for them. project: slug or id."))
    def reality_list(ctx: Context, project: str) -> dict:
        with session(ctx, "reality_list", project=project) as (conn, _c):
            p = _project_row(conn, project)
            caps = registry(conn, [p["id"]])
            return {"project": p["slug"], "capabilities": caps, "live": [c["key"] for c in caps if c["live"]],
                    "rule": "Never claim, link or promise what is not live here. Verification expires after "
                            f"{int(EXPIRY_HOURS)} h; live only via the platform probe or a reviewer."}

    @mcp.tool(name="reality_upsert", description=(
        "Add or correct a capability in a project's registry (never makes it live: the platform probe or a "
        "reviewer does). key: short id. fields: name (Czech: what the user can do), kind (capability | "
        "journey_step), position, status (unverified | test_only | mock | missing), url, access (public | "
        "password | internal), aliases (words that name it in a text), probe_mode (live: a passing anonymous "
        "probe of probe_url makes it live; protected: it must not open without a password; test: it runs on a "
        "password-protected test instance, the platform opens probe_url with probe_credential and marks it "
        "test_only + verified), probe_url, probe_expect (text the page must show), probe_credential (a "
        "credential you hold, e.g. kniha-test-basic-auth), deploy_ref (a commit the deployed test version must "
        "contain), notes."))
    def reality_upsert(ctx: Context, project: str, key: str, fields: dict[str, Any]) -> dict:
        with session(ctx, "reality_upsert", project=project, key=key) as (conn, c):
            return upsert(conn, c, project, key, fields)

    @mcp.tool(name="reality_submit_evidence", description=(
        "Show that a capability works for a real user: what you did as an outsider and what you saw (and the "
        "URL). A reviewer decides with reality_verify; your own claim never makes it live."))
    def reality_submit_evidence(ctx: Context, project: str, key: str, evidence: str, url: str = "") -> dict:
        with session(ctx, "reality_submit_evidence", project=project, key=key) as (conn, c):
            return submit_evidence(conn, c, project, key, evidence, url)

    @mcp.tool(name="reality_verify", description=(
        "Reviewer: accept or reject the evidence that a capability works (not your own). Accepting re-runs the "
        "anonymous probe of its URL: a link an outsider cannot open is never live."))
    def reality_verify(ctx: Context, project: str, key: str, accept: bool, note: str = "",
                       evidence_id: int | None = None) -> dict:
        with session(ctx, "reality_verify", project=project, key=key, accept=accept) as (conn, c):
            return verify(conn, c, project, key, accept, note, evidence_id)
