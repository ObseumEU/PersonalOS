"""Auto-discovery: items in the 1Password vault that are not registered yet, each
with a suggestion the owner applies with one click ("Zaregistrovat a přidělit").

A suggestion says what the item is (SSH login, API token, HTTP basic, DB login,
generic secret), which registry entries to make (name, field, env var, header,
hosts, commands), and which agents should get it. Deterministic rules decide
first: the category, field names and types, the title, the item's URLs and
host names found in its notes, and a table of known hosts of the owner's
network. Only when the rules cannot decide, one cheap model call
(claude-haiku-4-5, no tools) gets the item's title, category, field names and
types and URL hosts; never a value (this module never sees one: the provider
drops values before anything reaches it).
"""

import json
import logging
import re
import sqlite3
import unicodedata
from urllib.parse import urlsplit

log = logging.getLogger("pos.credentials.discover")

KINDS = ("ssh", "token", "basic", "db", "generic")
KIND_LABEL = {"ssh": "SSH přihlášení", "token": "API token", "basic": "HTTP basic", "db": "Databáze",
              "generic": "Obecné heslo"}
LLM_MODEL = "claude-haiku-4-5"
LLM_MAX_PER_CALL = 3  # model calls per discovery request; the rest waits for the next one (cached)

# The owner's known hosts: what an item is about -> where its credential may go and who works there.
KNOWN = [
    {"key": "home-assistant", "label": "Home Assistant",
     "match": ["home assistant", "homeassistant", "hass", "home-assistant"],
     "hosts": ["192.168.1.56", "homeassistant.local"],
     "http": ["192.168.1.56:8123", "homeassistant.local:8123"], "ssh": ["192.168.1.56", "homeassistant.local"],
     "agents": ["Home Assistant Specialist"], "names": {"ssh": "ha-ssh", "token": "home-assistant"},
     "ssh_tool": "ha_ssh"},
    {"key": "svr03", "label": "svr03", "match": ["svr03"], "hosts": ["192.168.1.108", "svr03"],
     "http": [], "ssh": ["192.168.1.108"], "agents": ["SRE"]},
    {"key": "srv186", "label": "server 186 (observability)", "match": ["grafana", "loki", "prometheus", "186"],
     "hosts": ["192.168.1.186", "grafana.obseum.cloud"], "http": ["grafana.obseum.cloud"],
     "ssh": ["192.168.1.186"], "agents": ["SRE", "Hlídač"]},
    {"key": "knowlage", "label": "knowlage", "match": ["knowlage"], "hosts": ["knowlage.obseum.cz"],
     "http": ["knowlage.obseum.cz"], "ssh": [], "agents": ["Knowlage Specialist"]},
    {"key": "nexus", "label": "Nexus", "match": ["nexus"], "hosts": ["nexus.obseum.cloud", "nexus-api.obseum.cloud"],
     "http": ["nexus.obseum.cloud", "nexus-api.obseum.cloud"], "ssh": [], "agents": ["Nexus Specialist"]},
    {"key": "litellm", "label": "LiteLLM", "match": ["litellm", "lite llm"], "hosts": ["litellm.obseum.cloud"],
     "http": ["litellm.obseum.cloud"], "ssh": [], "agents": ["Nexus Specialist", "CFO"]},
    {"key": "langfuse", "label": "Langfuse", "match": ["langfuse"], "hosts": ["langfuse.obseum.cloud"],
     "http": ["langfuse.obseum.cloud"], "ssh": [], "agents": ["SRE"]},
    {"key": "github", "label": "GitHub", "match": ["github"], "hosts": ["github.com", "api.github.com"],
     "http": ["api.github.com", "github.com"], "ssh": [], "agents": ["Software Engineer"],
     "commands": ["git push", "git fetch", "gh api"]},
    {"key": "discord", "label": "Discord", "match": ["discord"], "hosts": ["discord.com"],
     "http": ["discord.com"], "ssh": [], "agents": ["Community Manager"]},
    {"key": "linkedin", "label": "LinkedIn", "match": ["linkedin"], "hosts": ["api.linkedin.com"],
     "http": ["api.linkedin.com"], "ssh": [], "agents": ["Content & Brand"]},
]
# A dedicated tool that uses an SSH pair inside the API: its pseudo command and the tool grant it needs.
SSH_TOOLS = {"ha_ssh": "tool:ha_ssh"}
EXTRA_GRANTS = frozenset(SSH_TOOLS.values())  # the only non-credential grants a suggestion may carry

STOP = {"the", "and", "for", "api", "key", "token", "login", "password", "heslo", "user", "admin", "ssh", "http",
        "https", "www", "com", "cz", "cloud", "local", "server", "account", "ucet", "pro", "agent", "agents",
        "personalos", "obseum", "secret", "access", "credentials", "credential", "new", "old", "prod"}
USER_TITLES = {"username", "user", "login", "uzivatel", "uzivatelske jmeno", "email", "e-mail", "user name"}
PASSWORD_TITLES = {"password", "heslo", "pass", "passwd", "pwd"}
TOKEN_WORDS = {"token", "apikey", "api key", "api_key", "key", "secret", "pat", "bearer", "credential",
               "access token", "client secret", "webhook"}
DB_WORDS = {"postgres", "postgresql", "psql", "mysql", "mariadb", "mongodb", "mongo", "redis", "database", "db",
            "sql", "sqlite"}
HOST_RE = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}(?::\d+)?|(?:[a-z0-9-]+\.)+(?:cz|com|cloud|io|net|org|eu|dev|app|"
                     r"local|lan|home\.arpa)(?::\d+)?)\b", re.I)

_llm_cache: dict[str, dict | None] = {}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")[:56].strip("-")
    return out if len(out) >= 2 else f"cred-{out or 'x'}"


def env_of(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", norm(s)) if t}


# ------------------------------------------------------------------ the item's facts

def _secret(f: dict) -> bool:
    return norm(f.get("type", "")) in ("concealed", "password") or norm(f.get("title", "")) in PASSWORD_TITLES


def _user_field(fields: list[dict]) -> dict | None:
    return next((f for f in fields if f.get("id") == "username" or norm(f.get("title", "")) in USER_TITLES), None)


def _password_field(fields: list[dict]) -> dict | None:
    return next((f for f in fields if f.get("id") == "password" or norm(f.get("title", "")) in PASSWORD_TITLES),
                None)


def _token_field(fields: list[dict]) -> dict | None:
    for f in fields:
        t = norm(f.get("title", ""))
        if t in TOKEN_WORDS or tokens(t) & {"token", "apikey", "secret", "pat", "bearer", "key"}:
            if f.get("id") != "username":
                return f
    return None


def _hosts_of(item: dict) -> list[dict]:
    """[{host, port, scheme}] from the item's URLs and host names in its notes (no userinfo, no paths)."""
    out, seen = [], set()
    for u in item.get("urls") or []:
        try:
            p = urlsplit(u if "://" in u else f"https://{u}")
            host = (p.hostname or "").lower()
            port = p.port
        except ValueError:
            continue
        if host and (host, port) not in seen:
            seen.add((host, port))
            out.append({"host": host, "port": port, "scheme": (p.scheme or "https").lower()})
    for h in item.get("hosts") or []:
        host, _, port = str(h).lower().partition(":")
        key = (host, int(port) if port.isdigit() else None)
        if host and key not in seen:
            seen.add(key)
            out.append({"host": host, "port": key[1], "scheme": None})
    return out


def known_for(item: dict) -> dict | None:
    title = norm(item.get("title", ""))
    squashed = title.replace(" ", "")
    hosts = {h["host"] for h in _hosts_of(item)}
    for k in KNOWN:
        words = tokens(title)
        if any((" " in m and m in title) or m in words or (len(m) >= 6 and m.replace(" ", "") in squashed)
               for m in k["match"]):
            return k
        if hosts & set(k["hosts"]) or any(h.split(":")[0] in hosts for h in k["http"]):
            return k
    return None


def detect_kind(item: dict) -> tuple[str, bool, str]:
    """(kind, decided, why): the rules. `decided` False means the model may help."""
    fields = item.get("fields") or []
    title = norm(item.get("title", ""))
    words = tokens(title)
    cat = norm(item.get("category", ""))
    hosts = _hosts_of(item)
    user, pw, tok = _user_field(fields), _password_field(fields), _token_field(fields)
    secrets = [f for f in fields if _secret(f)]
    if "ssh" in words or cat in ("sshkey", "ssh key") or any(h["scheme"] == "ssh" or h["port"] == 22 for h in hosts):
        return "ssh", bool(pw or secrets), "SSH v názvu nebo adrese"
    if cat == "database" or words & DB_WORDS or any(norm(f.get("title", "")) == "database" for f in fields):
        return "db", bool(pw or secrets), "databázová položka"
    if cat in ("apicredentials", "api credential", "api credentials") or tok or words & {"token", "api", "apikey",
                                                                                          "pat", "webhook"}:
        return "token", bool(tok or secrets), "token nebo API klíč"
    if user and pw and (hosts or cat == "login"):
        return "basic", True, "jméno a heslo k webové adrese"
    if len(secrets) == 1:
        return "generic", False, "jedno tajné pole"
    return "generic", False, "pravidla nerozhodla"


# ------------------------------------------------------------------ the agents

def agents(conn: sqlite3.Connection) -> list[dict]:
    """Agents that can hold a credential: not people, not services, not archived, not the Access manager."""
    from ..access.service import AM_NAME
    from ..hr import store as hr_store

    hr_store.ensure_schema(conn)
    prof = hr_store.profiles(conn)
    rows = conn.execute("SELECT id, name, role, team FROM actors WHERE kind != 'human' AND archived_at IS NULL "
                        "AND runtime != 'service' AND name != ? ORDER BY name", (AM_NAME,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "role": r["role"], "team": r["team"],
             "purpose": (prof[r["id"]]["purpose"] if r["id"] in prof else "") or ""} for r in rows]


def match_agents(text: str, hosts: list[str], roster: list[dict], known: dict | None = None,
                 limit: int = 3) -> list[dict]:
    """Who should get it: the known table's agents first, then agents whose name or
    purpose names what the item is about. [{id, name, why}]."""
    out: list[dict] = []
    by_name = {norm(a["name"]): a for a in roster}
    if known:
        for n in known.get("agents", []):
            a = by_name.get(norm(n))
            if a:
                out.append({"id": a["id"], "name": a["name"], "why": f"spravuje {known['label']}"})
    if out:
        return out[:limit]
    words = {w for w in tokens(text) if len(w) >= 3 and w not in STOP}
    words |= {p for h in hosts for p in re.split(r"[.:]", norm(h)) if len(p) >= 4 and p not in STOP and not p.isdigit()}
    if not words:
        return []
    scored = []
    for a in roster:
        name_t = tokens(a["name"]) | {norm(a["name"]).replace(" ", "")}
        purpose = norm(" ".join(filter(None, [a.get("purpose"), a.get("role"), a.get("team")])))
        hit_name = sorted(w for w in words if w in name_t)
        hit_purpose = sorted(w for w in words if re.search(rf"\b{re.escape(w)}", purpose))
        score = 3 * len(hit_name) + len(hit_purpose)
        if score:
            scored.append((score, a, hit_name or hit_purpose))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    best = scored[0][0]
    return [{"id": a["id"], "name": a["name"], "why": f"v roli: {', '.join(hits[:2])}"}
            for s, a, hits in scored if s >= max(2, best - 1)][:limit] or \
           [{"id": scored[0][1]["id"], "name": scored[0][1]["name"], "why": f"v roli: {', '.join(scored[0][2][:2])}"}]


# ------------------------------------------------------------------ the suggestion

def _unique(name: str, taken: set[str]) -> str:
    base, n, out = name, 2, name
    while out in taken:
        out = f"{base}-{n}"
        n += 1
    taken.add(out)
    return out


def _http_hosts(item: dict, known: dict | None) -> list[str]:
    if known and known.get("http"):
        return list(known["http"])
    out = []
    for h in _hosts_of(item):
        private = re.match(r"^(10|127|192\.168|172\.(1[6-9]|2\d|3[01]))\.", h["host"]) or h["host"].endswith(
            (".local", ".lan", ".home.arpa"))
        if h["port"] and h["port"] not in (80, 443) and (private or h["scheme"] == "http"):
            out.append(f"{h['host']}:{h['port']}")
        else:
            out.append(h["host"])
    return sorted(set(out))


def _plain_hosts(item: dict, known: dict | None, kind: str) -> list[str]:
    if known and known.get(kind):
        return list(known[kind])
    return sorted({h["host"] for h in _hosts_of(item)})


def build(item: dict, kind: str, roster: list[dict], taken: set[str], *, decided: bool = True, why: str = "",
          source: str = "rules", agent_names: list[str] | None = None) -> dict:
    """The suggestion for one vault item as a given kind."""
    fields = item.get("fields") or []
    known = known_for(item)
    title = item.get("title", "")
    user, pw, tok = _user_field(fields), _password_field(fields), _token_field(fields)
    secrets = [f for f in fields if _secret(f)]
    creds: list[dict] = []
    extra: list[str] = []
    usage = ""
    base = slug(re.sub(r"\bssh\b", " ", norm(title)))
    names = (known or {}).get("names", {})

    def cred(f: dict, name: str, role: str, **kw) -> dict:
        d = {"field": f.get("title"), "field_id": f.get("id"), "op_ref": f.get("op_ref"), "role": role,
             "name": _unique(name, taken), "env_var": None, "header": None, "allowed_hosts": [],
             "allowed_tools": [], "allowed_commands": [], "description": ""}
        d.update(kw)
        return d

    if kind == "ssh":
        secret = pw or (secrets[0] if secrets else None)
        hosts = _plain_hosts(item, known, "ssh")
        tool = (known or {}).get("ssh_tool")
        name = names.get("ssh") or (f"{base}-ssh" if base != "cred-x" else "ssh")
        if tool:
            common = {"allowed_hosts": hosts, "allowed_tools": ["command"], "allowed_commands": [tool]}
            usage = f"SSH přes nástroj {tool} (uživatel + heslo z 1Password)"
            extra.append(SSH_TOOLS[tool])
        else:
            common = {"allowed_hosts": hosts, "allowed_tools": ["command"],
                      "allowed_commands": ["sshpass -e ssh", "sshpass -e scp"]}
            usage = "SSH příkazem sshpass -e ssh (heslo v proměnné SSHPASS)"
        if secret:
            creds.append(cred(secret, name, "password", env_var=None if tool else "SSHPASS",
                              description=f"{title}: SSH heslo" + (f" pro {', '.join(hosts)}" if hosts else ""),
                              **common))
        if user:
            creds.append(cred(user, f"{creds[0]['name'] if creds else name}-user", "user",
                              env_var=None if tool else env_of(f"{name}-user"),
                              description=f"{title}: SSH uživatel", **common))
    elif kind == "token":
        secret = tok or next((f for f in secrets if f is not pw), None) or pw or (fields[0] if fields else None)
        hosts = _http_hosts(item, known)
        name = names.get("token") or base
        env = env_of(name) if env_of(name).endswith(("TOKEN", "KEY")) else env_of(f"{name}-token")
        cmds = list((known or {}).get("commands", []))
        if secret:
            creds.append(cred(secret, name, "token", env_var=env, header="Authorization: Bearer {value}",
                              allowed_hosts=hosts, allowed_tools=[] if hosts and cmds else (["http"] if hosts else ["command"]),
                              allowed_commands=cmds, description=f"{title}: API token"
                              + (f" pro {', '.join(hosts)}" if hosts else "")))
        usage = "hlavička Authorization: Bearer … (credential_http)" + (f" nebo proměnná {env}" if cmds or not hosts else "")
    elif kind == "basic":
        hosts = _http_hosts(item, known)
        if pw:
            creds.append(cred(pw, base, "password", allowed_hosts=hosts, allowed_tools=["http"],
                              description=f"{title}: heslo (HTTP basic)"))
        if user:
            creds.append(cred(user, f"{creds[0]['name'] if creds else base}-user", "user", allowed_hosts=hosts,
                              allowed_tools=["http"], description=f"{title}: uživatel (HTTP basic)"))
        host = hosts[0] if hosts else "host"
        if creds:
            u = creds[-1]["name"] if len(creds) > 1 else "…"
            usage = f"HTTP basic: https://{{{{cred:{u}}}}}:{{{{cred:{creds[0]['name']}}}}}@{host}/…"
    elif kind == "db":
        text = norm(title + " " + " ".join(item.get("urls") or []))
        pg = not re.search(r"mysql|maria", text)
        env, cmds = ("PGPASSWORD", ["psql", "pg_dump"]) if pg else ("MYSQL_PWD", ["mysql", "mysqldump"])
        hosts = _plain_hosts(item, known, "hosts")
        secret = pw or (secrets[0] if secrets else None)
        if secret:
            creds.append(cred(secret, base, "password", env_var=env, allowed_hosts=hosts, allowed_tools=["command"],
                              allowed_commands=cmds, description=f"{title}: heslo k databázi"))
        if user:
            creds.append(cred(user, f"{creds[0]['name'] if creds else base}-user", "user",
                              env_var="PGUSER" if pg else env_of(f"{base}-user"), allowed_hosts=hosts,
                              allowed_tools=["command"], allowed_commands=cmds, description=f"{title}: uživatel"))
        usage = f"{cmds[0]} s heslem v proměnné {env}"
    else:  # generic
        secret = pw or tok or (secrets[0] if secrets else None) or (fields[0] if fields else None)
        hosts = _http_hosts(item, known)
        if secret:
            creds.append(cred(secret, base, "secret", env_var=env_of(base), allowed_hosts=hosts,
                              allowed_tools=["command"], description=f"{title}: {secret.get('title')}"))
        usage = f"proměnná {env_of(base)} v příkazu (doplň povolené příkazy v Pokročilém)"
    hosts_all = sorted({h for c in creds for h in c["allowed_hosts"]})
    if agent_names is not None:
        by = {norm(a["name"]): a for a in roster}
        picks = [{"id": by[norm(n)]["id"], "name": by[norm(n)]["name"], "why": "doporučil model"}
                 for n in agent_names if norm(n) in by]
    else:
        picks = match_agents(title + " " + " ".join(f.get("title", "") for f in fields), hosts_all, roster, known)
    return {"item_id": item.get("id"), "title": title, "category": item.get("category"), "kind": kind,
            "kind_label": KIND_LABEL[kind], "decided": decided, "source": source, "why": why,
            "known": known["label"] if known else None, "usage": usage, "hosts": hosts_all,
            "credentials": creds, "extra_grants": extra, "agents": picks,
            "fields": [{"id": f.get("id"), "title": f.get("title"), "type": f.get("type"),
                        "section": f.get("section"), "op_ref": f.get("op_ref")} for f in fields]}


def suggest(conn: sqlite3.Connection, item: dict, *, roster: list[dict] | None = None, taken: set[str] | None = None,
            kind: str | None = None, use_llm: bool = True) -> dict:
    roster = agents(conn) if roster is None else roster
    taken = {r[0] for r in conn.execute("SELECT name FROM credentials")} if taken is None else taken
    if kind:
        if kind not in KINDS:
            raise ValueError(f"kind: one of {KINDS}")
        return build(item, kind, roster, taken, why="zvoleno majitelem", source="owner")
    k, decided, why = detect_kind(item)
    if decided or not use_llm:
        return build(item, k, roster, taken, decided=decided, why=why)
    got = llm_suggest(conn, item, roster)
    if got and got.get("kind") in KINDS:
        return build(item, got["kind"], roster, taken, decided=True, why=got.get("reason") or "podle modelu",
                     source="llm", agent_names=got.get("agents") or [])
    return build(item, k, roster, taken, decided=False, why=why)


# ------------------------------------------------------------------ the model, only when rules cannot decide

def llm_facts(item: dict) -> dict:
    """What the model may see: title, category, field names and types, URL hosts. Never a value."""
    return {"title": item.get("title", "")[:200], "category": item.get("category", ""),
            "fields": [{"title": f.get("title", "")[:80], "type": f.get("type", "")} for f in item.get("fields") or []][:20],
            "hosts": [h["host"] + (f":{h['port']}" if h["port"] else "") for h in _hosts_of(item)][:10]}


def llm_suggest(conn: sqlite3.Connection, item: dict, roster: list[dict]) -> dict | None:
    facts = llm_facts(item)
    key = json.dumps([facts, [a["name"] for a in roster]], sort_keys=True)
    if key in _llm_cache:
        return _llm_cache[key]
    if sum(1 for v in _llm_cache.values() if v is not None) > 500:
        _llm_cache.clear()
    try:
        out = _llm(conn, facts, roster)
    except Exception as e:  # noqa: BLE001 - the rules' answer stands
        log.info("credential suggestion model failed: %s", e)
        out = None
    _llm_cache[key] = out
    return out


def _llm(conn: sqlite3.Connection, facts: dict, roster: list[dict]) -> dict | None:
    """One tool-less claude-haiku-4-5 call, recorded as a run of the Access manager (or the owner)."""
    from .. import actors, integrations, runner
    from ..access.service import manager_id

    if not runner.available("claude"):
        return None
    team = "\n".join(f"- {a['name']}: {a['purpose'][:160]}" for a in roster)
    prompt = (
        "Classify one item of a password manager for an AI agent platform. You see only metadata "
        "(never secret values). Answer with JSON only: "
        '{"kind": "ssh|token|basic|db|generic", "agents": ["<agent name>", ...], "reason": "<Czech, max 12 words>"}.\n'
        "kind: ssh = SSH login to a host; token = API token/key for HTTP; basic = user+password for a web API; "
        "db = database login; generic = anything else. agents: at most 2 names from the list who need it for "
        "their role, [] if none fits.\n\n"
        f"Item: {json.dumps(facts, ensure_ascii=False)}\n\nAgents:\n{team}\n")
    integrations.install()
    who = manager_id(conn) or actors.owner_id(conn)
    res = runner.run(conn, runner.RunRequest(who, "cred_suggest", prompt, engine="claude", model=LLM_MODEL,
                                             timeout_s=60))
    if res.status != "ok":
        return None
    m = re.search(r"\{.*\}", res.output or "", re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    return {"kind": str(data.get("kind", "")).strip().lower(),
            "agents": [str(x) for x in (data.get("agents") or [])][:3],
            "reason": str(data.get("reason") or "")[:120]}


# ------------------------------------------------------------------ recommended agents for a registered credential

def recommend_for(cred: dict, roster: list[dict]) -> list[dict]:
    item = {"title": cred["name"].replace("-", " ") + " " + (cred.get("description") or "")[:200],
            "hosts": [h.lstrip("*.") for h in cred.get("allowed_hosts", [])], "fields": []}
    return match_agents(item["title"], item["hosts"], roster, known_for(item))
