"""Browser use for agents: the policy, the audit trail and the screenshots.

Agents drive a real browser through Playwright MCP, behind the worker's guard
proxy (worker/pos_worker/browser_guard.py). The guard asks `check` before every
tool call and reports each one to `record`.

The owner's rule (2026-09-25): browsing, reading, logged-in pages and filling in
forms need no approval. Approval is needed only for what the constitution calls
irreversible or outward: paying or buying, sending a message or e-mail on the
owner's behalf, deleting, and changing account or security settings. Banking
and payment sites always ask first. Everything is in the audit log, with a
screenshot of each action; page content reaches the agent as untrusted data.
Typed text is never stored (it may be a password).

The constitution as amended on 2026-09-27 (Ú1): ordinary outbound work goes out
without approval and is audited (every action has a screenshot), so submitting,
replying, commenting, posting and uploading on ordinary sites are free. Approval
stays only for the three kinds the constitution keeps for the owner: money (paying,
buying, ordering, transferring, payment details), commitments (signing a contract,
accepting an offer) and posting on the owner's personal channels (LinkedIn,
personal social networks). Deleting and changing account or security settings
still ask (the owner's rule of 2026-09-25). An agent's action hosts are its
`scope:browser:<host[:port]>` grants (the owner's; e.g. the Home Assistant
Specialist's own HA UI): there it acts freely. Banking sites always ask.

The same check covers computer use (`computer_*` tools, the desktop sandbox):
`tool:computer` instead of `tool:browser` / `browser:use`.
"""

import base64
import contextlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import approvals, audit
from .core import Ctx

# Sites where any visit asks the owner first: banks and payment services.
APPROVAL_DOMAINS = (
    "csob.cz", "kb.cz", "csas.cz", "servis24.cz", "georgeapp.cz", "fio.cz", "rb.cz", "moneta.cz", "airbank.cz",
    "mbank.cz", "unicreditbank.cz", "creditas.cz", "equabank.cz", "trinitybank.cz", "revolut.com", "wise.com",
    "paypal.com", "stripe.com", "gopay.com", "comgate.cz", "thepay.cz", "coinbase.com", "binance.com",
)
# Ú1 money: paying, buying, ordering, transferring.
MONEY_ACTION = re.compile(
    r"\b(buy|buy now|purchase|order now|place order|checkout|check out|pay|payment|pay now|subscribe|upgrade plan|"
    r"donate|transfer|send money|add funds|top up|zaplatit|platba|zaplaťte|koupit|objednat|objednávka|předplatit|"
    r"převést|převod|dobít)\b",
    re.IGNORECASE,
)
# Ú1 commitments: signing, accepting a binding offer.
COMMIT_ACTION = re.compile(
    r"\b(sign contract|sign agreement|e-?sign|sign and submit|sign now|accept offer|accept quote|accept proposal|"
    r"podepsat|podepsat smlouvu|přijmout nabídku|potvrdit objednávku)\b",
    re.IGNORECASE,
)
# The owner's rule of 2026-09-25 (not outbound): deleting and account or security settings.
DESTRUCTIVE_ACTION = re.compile(
    r"\b(delete|remove|erase|close account|deactivate|change password|reset password|two-factor|2fa|"
    r"security settings|smazat|odstranit|zrušit účet|změnit heslo|zabezpečení)\b",
    re.IGNORECASE,
)
# Kept for callers that ask "is this risky at all".
RISKY_ACTION = re.compile("|".join(r.pattern for r in (MONEY_ACTION, COMMIT_ACTION, DESTRUCTIVE_ACTION)),
                          re.IGNORECASE)
RISKY_SCRIPT = re.compile(r"fetch\(|XMLHttpRequest|sendBeacon|\.submit\(|\.click\(|window\.open\(|navigator\.", re.I)
# Submitting / posting: free on ordinary sites (Ú1), asks on the owner's personal channels.
SUBMIT_ACTION = re.compile(
    r"\b(submit|send|post|publish|reply|comment|tweet|share|upload|repost|like|follow|connect|endorse|"
    r"odeslat|poslat|zveřejnit|publikovat|sdílet|nahrát|komentovat|přidat|sledovat)\b", re.IGNORECASE)
# ... except these, which only read: searching, filtering, logging in.
READ_ONLY_ACTION = re.compile(
    r"\b(search|find|filter|go|lookup|look up|hledat|vyhledat|najít|filtr|log ?in|sign ?in|přihlásit|"
    r"přihlášení|next page|další|more|zobrazit|show)\b", re.IGNORECASE)
COMPUTER_TOOLS = {"computer_left_click", "computer_right_click", "computer_middle_click", "computer_double_click",
                  "computer_triple_click", "computer_left_click_drag", "computer_type", "computer_key",
                  "computer_scroll", "computer_mouse_move", "computer_open_url"}
ACTION_TOOLS = {"browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
                "browser_navigate", "browser_evaluate", "browser_file_upload", "browser_drag", "browser_handle_dialog"}


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _hostport(url: str | None) -> str:
    p = urlparse(url or "")
    try:
        port = p.port
    except ValueError:
        port = None
    return f"{(p.hostname or '').lower()}:{port}" if port else (p.hostname or "").lower()


def on_action_host(url: str | None, action_hosts) -> bool:
    """Is this page on one of the agent's action hosts? An entry with a port matches that
    host:port exactly; one without matches the host and its subdomains on any port."""
    host, hp = _host(url), _hostport(url)
    for h in action_hosts or ():
        h = str(h).strip().lower()
        if not h or not host:
            continue
        if ":" in h:
            if hp == h:
                return True
        elif host == h or host.endswith("." + h):
            return True
    return False


def _submits(label: str) -> bool:
    return bool(label) and bool(SUBMIT_ACTION.search(label)) and not READ_ONLY_ACTION.search(label)


def approval_domains() -> tuple[str, ...]:
    extra = tuple(d.strip().lower() for d in os.environ.get("POS_BROWSER_APPROVAL_DOMAINS", "").split(",") if d.strip())
    return APPROVAL_DOMAINS + extra


def personal_channel(host: str) -> bool:
    """One of the owner's personal channels (LinkedIn, personal socials): posting there is his call."""
    from .guard.policy import PERSONAL_CHANNEL_HOSTS

    return bool(host) and _matches(host, PERSONAL_CHANNEL_HOSTS)


def decide(tool: str, args: dict, *, url: str | None = None, last_field: str | None = None,
           allow_hosts: list[str] | None = None, action_hosts: list[str] | None = None,
           element: str | None = None) -> tuple[str, str]:
    """("allow" | "approval", reason) for one browser (or computer) tool call.
    `url` is the page the agent is on; `last_field` the element it typed into last;
    `action_hosts` where the agent may do anything; `element` what a computer
    click lands on (the desktop inspects it)."""
    target = args.get("url") if tool in ("browser_navigate", "computer_open_url") else url
    host = _host(target)
    if host and _matches(host, approval_domains()):
        return "approval", f"{host} is a banking or payment site"
    if tool.startswith("computer_"):
        return _decide_computer(tool, args, url=url, last_field=last_field, action_hosts=action_hosts,
                                element=element)
    if on_action_host(target, action_hosts):
        return "allow", "one of this agent's action hosts"
    if tool == "browser_navigate" and allow_hosts and host and not _matches(host, allow_hosts):
        return "approval", f"{host} is outside this agent's usual sites"
    element = str(args.get("element") or args.get("name") or "")
    if tool in ("browser_click", "browser_select_option"):
        risk = _risky(element)
        if risk:
            return "approval", risk
    if tool == "browser_fill_form":
        names = " ".join(str(f.get("name", "")) for f in args.get("fields") or [])
        if re.search(r"card number|cvv|cvc|číslo karty|iban", names, re.IGNORECASE):
            return "approval", "the form asks for payment details (money, Ú1)"
    if tool == "browser_evaluate" and RISKY_SCRIPT.search(str(args.get("function") or "")):
        return "approval", "the script sends requests or clicks by itself"
    # Ú1: posting on the owner's personal channels asks; on any other site it is ordinary work.
    if personal_channel(host):
        where = host
        if tool == "browser_click" and _submits(element):
            return "approval", f"'{element[:80]}' posts on {where}, one of the owner's personal channels"
        if tool == "browser_type" and args.get("submit") and not READ_ONLY_ACTION.search(element):
            return "approval", f"submitting '{element[:80]}' posts on {where}, one of the owner's personal channels"
        if tool == "browser_press_key" and str(args.get("key", "")).lower() == "enter" and last_field \
                and not READ_ONLY_ACTION.search(last_field):
            return "approval", f"Enter in '{last_field[:80]}' posts on {where}, one of the owner's personal channels"
        if tool == "browser_file_upload" and args.get("paths"):
            return "approval", f"uploading to {where} posts on one of the owner's personal channels"
    return "allow", "ok"


def _risky(label: str) -> str | None:
    """Why a click on this label asks first (money, a commitment, deleting or account settings), or None."""
    if not label:
        return None
    if MONEY_ACTION.search(label):
        return f"'{label[:80]}' looks like paying or buying (money, Ú1)"
    if COMMIT_ACTION.search(label):
        return f"'{label[:80]}' looks like signing or accepting a binding offer (commitment, Ú1)"
    if DESTRUCTIVE_ACTION.search(label):
        return f"'{label[:80]}' looks like deleting or changing account settings"
    return None


def _decide_computer(tool: str, args: dict, *, url: str | None, last_field: str | None,
                     action_hosts: list[str] | None, element: str | None) -> tuple[str, str]:
    """The desktop sandbox: the same rule, with what the desktop's browser tells
    about the page (its URL, the element under the pointer, the field typed into)."""
    if tool == "computer_open_url" or on_action_host(url, action_hosts):
        return "allow", "ok"
    host = _host(url)
    label = element or ""
    if tool in ("computer_left_click", "computer_double_click", "computer_triple_click") and label:
        risk = _risky(label)
        if risk:
            return "approval", risk
        if personal_channel(host) and _submits(label):
            return "approval", f"'{label[:80]}' posts on {host}, one of the owner's personal channels"
    if tool == "computer_key" and personal_channel(host):
        keys = str(args.get("text") or "").lower().replace(" ", "")
        if any(k in ("return", "enter", "kp_enter") for k in keys.split("+")) and last_field \
                and not READ_ONLY_ACTION.search(last_field):
            return "approval", f"Enter in '{last_field[:80]}' posts on {host}, one of the owner's personal channels"
    return "allow", "ok"


def redact(tool: str, args: dict) -> dict:
    """Arguments safe to store: typed text and form values are never kept."""
    out = {}
    for k, v in (args or {}).items():
        if k in ("text", "value", "values"):
            out[k] = f"<{len(str(v))} chars>"
        elif k == "fields" and isinstance(v, list):
            out[k] = [{**{kk: vv for kk, vv in f.items() if kk not in ("value",)}, "value": "<hidden>"}
                      for f in v if isinstance(f, dict)]
        else:
            out[k] = v if len(str(v)) < 500 else str(v)[:500] + "…"
    return out


def shots_dir(data_dir: Path) -> Path:
    return data_dir / "files" / "browser"


def _image(b64: str | None, limit: int = 8 * 1024 * 1024) -> tuple[bytes, str] | None:
    """(bytes, suffix) of a PNG or JPEG screenshot; None for anything else."""
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        return None
    if len(raw) > limit:
        return None
    if raw.startswith(b"\x89PNG"):
        return raw, ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return raw, ".jpg"
    return None


def save_screenshot(data_dir: Path, actor_id: int, tool: str, b64: str | None) -> str | None:
    got = _image(b64)
    if got is None:
        return None
    raw, suffix = got
    now = datetime.now(timezone.utc)
    name = f"{now.strftime('%H%M%S%f')}-{re.sub(r'[^a-z_]', '', tool)}{suffix}"
    rel = Path(str(actor_id)) / now.strftime("%Y%m%d") / name
    path = shots_dir(data_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return rel.as_posix()


def check(conn: sqlite3.Connection, ctx: Ctx, data_dir: Path, body: dict) -> dict:
    """The guard's question before a tool call. With `dry_run` it only answers;
    otherwise an approval is requested, with the screenshot the guard took."""
    tool = str(body.get("tool") or "")
    args = body.get("args") or {}
    decision, reason = decide(tool, args, url=body.get("url"), last_field=body.get("last_field"),
                              allow_hosts=body.get("allow_hosts") or None,
                              action_hosts=action_hosts(conn, ctx.actor_id),
                              element=str(body.get("element") or "") or None)
    out = {"decision": decision, "reason": reason}
    if decision == "approval" and not body.get("dry_run"):
        shot = save_screenshot(data_dir, ctx.actor_id, tool, body.get("screenshot"))
        a = approvals.request(conn, ctx, f"browser: {tool.removeprefix('browser_')}", {
            "why": reason, "url": body.get("url"), "tool": tool, "args": redact(tool, args), "screenshot": shot,
            **({"element": str(body["element"])[:200]} if body.get("element") else {}),
        }, task_id=body.get("task_id"))
        out["approval_id"] = a["id"]
    conn.commit()
    return out


def _run_ctx(conn: sqlite3.Connection, ctx: Ctx, run_id) -> Ctx:
    """The audit entry belongs to the run (the agent page's trace shows it), when it is this agent's."""
    try:
        rid = int(run_id or 0)
    except (TypeError, ValueError):
        return ctx
    row = conn.execute("SELECT actor_id FROM runs WHERE id = ?", (rid,)).fetchone() if rid else None
    return Ctx(ctx.actor_id, via=ctx.via, run_id=rid) if row and row["actor_id"] == ctx.actor_id else ctx


def record(conn: sqlite3.Connection, ctx: Ctx, data_dir: Path, body: dict) -> dict:
    tool = str(body.get("tool") or "")
    shot = save_screenshot(data_dir, ctx.actor_id, tool, body.get("screenshot"))
    kind = "computer" if tool.startswith("computer_") else "browser"
    extra = {k: str(body[k])[:300] for k in ("download", "note") if body.get(k)}
    rctx = _run_ctx(conn, ctx, body.get("run_id"))
    audit.log(conn, rctx, f"{kind}:{tool.removeprefix(kind + '_')}",
              "task" if body.get("task_id") else None,
              body.get("task_id"), url=body.get("url"), args=redact(tool, body.get("args") or {}),
              ok=bool(body.get("ok", True)), screenshot=shot, approval_id=body.get("approval_id"), **extra)
    conn.commit()
    if shot and rctx.run_id:  # the last step is also the live view's frame
        save_live(data_dir, rctx.run_id, body.get("screenshot"),
                  {"url": body.get("url"), "kind": kind, "step": tool.removeprefix(kind + "_")})
    return {"screenshot": shot}


def screenshot_path(data_dir: Path, rel: str) -> Path | None:
    base = shots_dir(data_dir).resolve()
    p = (base / rel).resolve()
    return p if p.is_file() and base in p.parents and p.suffix in (".png", ".jpg") else None


# ------------------------------------------------------------------ the live view

LIVE_WATCH_S = 20  # a run page polled this recently counts as watched: the guard sends frames


def live_dir(data_dir: Path) -> Path:
    return shots_dir(data_dir) / "live"


def save_live(data_dir: Path, run_id: int, b64: str | None, meta: dict) -> bool:
    """The newest frame of a run's browser or desktop (one file per run, overwritten)."""
    got = _image(b64, 4 * 1024 * 1024)
    if got is None:
        return False
    raw, suffix = got
    d = live_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{int(run_id)}.tmp"
    tmp.write_bytes(raw)
    tmp.replace(d / f"{int(run_id)}.img")
    info = {k: (str(v)[:500] if v is not None else None) for k, v in meta.items()}
    info.update(mime="image/png" if suffix == ".png" else "image/jpeg",
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    (d / f"{int(run_id)}.json").write_text(json.dumps(info), encoding="utf-8")
    return True


def live_frame(data_dir: Path, run_id: int) -> tuple[Path, dict] | None:
    d = live_dir(data_dir)
    img, meta = d / f"{int(run_id)}.img", d / f"{int(run_id)}.json"
    if not img.is_file():
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    return img, info


def mark_watching(data_dir: Path, run_id: int) -> None:
    d = live_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{int(run_id)}.watch").touch()


def watching(data_dir: Path, run_id: int) -> bool:
    try:
        age = datetime.now().timestamp() - (live_dir(data_dir) / f"{int(run_id)}.watch").stat().st_mtime
    except (OSError, ValueError):
        return False
    return age < LIVE_WATCH_S


def prune_live(data_dir: Path, keep_s: float = 7 * 86400) -> None:
    d = live_dir(data_dir)
    if not d.is_dir():
        return
    cutoff = datetime.now().timestamp() - keep_s
    for f in d.iterdir():
        with contextlib.suppress(OSError):
            if f.stat().st_mtime < cutoff:
                f.unlink()


# ------------------------------------------------------------------ kept logins (browser:profile)

PROFILE_GRANT = "browser:profile"
PROFILE_MAX_BYTES = 5 * 1024 * 1024


def may_keep_profile(conn: sqlite3.Connection, actor_id: int) -> bool:
    """The owner's grant browser:profile (or the older scope:browser-profile:<name>)."""
    from . import agents

    perms = agents.permissions_of(conn, actor_id)
    return PROFILE_GRANT in perms or any(p.startswith("scope:browser-profile:") for p in perms)


def profiles_dir(data_dir: Path) -> Path:
    return data_dir / "browser-profiles"


def _profile_key(secret: str, actor_id: int) -> bytes:
    """A key per agent, derived from the server's secret (POS_BROWSER_PROFILE_KEY, else the session
    secret): one agent's saved logins can never be opened with another's key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    master = (os.environ.get("POS_BROWSER_PROFILE_KEY") or secret or "").encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"pos-browser-profile-v1",
                info=f"agent:{int(actor_id)}".encode()).derive(master)


def save_profile(data_dir: Path, secret: str, actor_id: int, state: dict) -> int:
    """The agent's cookies and site storage (Playwright's storage state), encrypted (AES-GCM) at rest."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = json.dumps(state, separators=(",", ":")).encode()
    if len(raw) > PROFILE_MAX_BYTES:
        raise ValueError(f"the browser profile is too big ({len(raw) // 1024} KB)")
    nonce = os.urandom(12)
    aad = f"agent:{int(actor_id)}".encode()
    blob = b"PBP1" + nonce + AESGCM(_profile_key(secret, actor_id)).encrypt(nonce, raw, aad)
    d = profiles_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{int(actor_id)}.tmp"
    tmp.write_bytes(blob)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(d / f"{int(actor_id)}.bin")
    return len(raw)


def load_profile(data_dir: Path, secret: str, actor_id: int) -> dict | None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = (profiles_dir(data_dir) / f"{int(actor_id)}.bin").read_bytes()
    except OSError:
        return None
    if not blob.startswith(b"PBP1") or len(blob) < 32:
        return None
    try:
        raw = AESGCM(_profile_key(secret, actor_id)).decrypt(blob[4:16], blob[16:], f"agent:{int(actor_id)}".encode())
    except InvalidTag:  # another key (the secret changed) or another agent's file: as if there were none
        return None
    return json.loads(raw)


def profile_info(data_dir: Path, secret: str, actor_id: int) -> dict:
    """For the owner: whether logins are kept and for which sites (never the cookies themselves)."""
    path = profiles_dir(data_dir) / f"{int(actor_id)}.bin"
    if not path.is_file():
        return {"exists": False}
    state = load_profile(data_dir, secret, actor_id) or {}
    sites = {str(c.get("domain") or "").lstrip(".") for c in state.get("cookies") or []}
    sites |= {urlparse(str(o.get("origin") or "")).hostname or "" for o in state.get("origins") or []}
    return {"exists": True, "bytes": path.stat().st_size, "sites": sorted(s for s in sites if s),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}


def clear_profile(conn: sqlite3.Connection, ctx: Ctx, data_dir: Path, actor_id: int) -> bool:
    path = profiles_dir(data_dir) / f"{int(actor_id)}.bin"
    existed = path.is_file()
    with contextlib.suppress(OSError):
        path.unlink()
    audit.log(conn, ctx, "browser:profile_cleared", "actor", actor_id, existed=existed)
    conn.commit()
    return existed


# ------------------------------------------------------------------ grants (tool:browser, tool:computer)

# Capabilities for the worker's own MCP servers (not pos tools); pos.access knows them.
WORKER_TOOLS = {"browser": "a headless browser (Playwright MCP behind the guard; paying, signing, deleting "
                           "and posting on the owner's personal channels ask)",
                "computer": "a desktop sandbox (screen, mouse, keyboard) for tasks that need a real GUI"}
# scope:browser:<host[:port]>: the agent does anything there, deleting included (owner only).
# browser:profile (older: scope:browser-profile:<name>): the agent's logins kept between runs, encrypted
# by PersonalOS (owner only; the owner clears them on the agent's page).
SCOPES = ("browser", "browser-profile")


def may_browse(conn: sqlite3.Connection, actor_id: int) -> bool:
    from . import agents

    return agents.has_permission(conn, actor_id, "tool:browser") or agents.has_permission(conn, actor_id, "browser:use")


def may_use_computer(conn: sqlite3.Connection, actor_id: int) -> bool:
    from . import agents

    return agents.has_permission(conn, actor_id, "tool:computer")


def action_hosts(conn: sqlite3.Connection, actor_id: int) -> list[str]:
    """The hosts where this agent submits without approval: its scope:browser:<host> grants."""
    from . import agents

    return sorted(p.split(":", 2)[2] for p in agents.permissions_of(conn, actor_id)
                  if p.startswith("scope:browser:") and len(p.split(":", 2)) == 3)


# The owner's decision of 2026-09-27 (who gets what on day one; the Access manager grants more).
SEED_BROWSER = ("CEO", "Chief of Staff", "CTO", "SRE", "Home Assistant Specialist", "Nexus Specialist",
                "Knowlage Specialist", "Security Engineer", "Head of Growth", "Content & Brand",
                "Head of Customer Success", "CFO")
SEED_COMPUTER = ("Home Assistant Specialist", "SRE")
# Action hosts: the HA Specialist's own Home Assistant (full admin, the owner's decision) and
# the company's LAN apps for the specialists who run them.
SEED_ACTION_HOSTS = {
    "Home Assistant Specialist": ("192.168.1.56:8123", "homeassistant.local:8123"),
    "SRE": ("192.168.1.108", "192.168.1.186", "grafana.obseum.cloud"),
    "Nexus Specialist": ("nexus.obseum.cloud", "nexus-api.obseum.cloud"),
    "Knowlage Specialist": ("knowlage.obseum.cz",),
}
SEED_STATE = "browser.grants_seeded"


def seed_plan() -> list[tuple[str, str, str]]:
    out = [(n, "tool:browser", "prohlížeč pro weby bez API (rozhodnutí majitele 2026-09-27)") for n in SEED_BROWSER]
    out += [(n, "tool:computer", "desktop sandbox pro úkoly s GUI (rozhodnutí majitele 2026-09-27)")
            for n in SEED_COMPUTER]
    out += [(n, f"scope:browser:{h}", "vlastní aplikace: odeslání formuláře bez schválení (rozhodnutí majitele)")
            for n, hosts in SEED_ACTION_HOSTS.items() for h in hosts]
    return out


def ensure_grants(conn: sqlite3.Connection) -> list[str]:
    """Day one of browser and computer use: the grants above, once each (a grant the owner or
    the Access manager revokes later stays revoked). An agent that does not exist yet is
    skipped and gets its grants on a later start."""
    from . import actors, settings_store
    from .access import service as access
    from .access import store

    store.ensure_schema(conn)
    done_before = set(settings_store.get(conn, SEED_STATE) or [])
    owner = actors.owner_id(conn)
    done = []
    for name, cap, why in seed_plan():
        key = f"{name}|{cap}"
        if key in done_before:
            continue
        row = actors.find_by_name(conn, name)
        if row is None:
            continue
        if not store.seeded(conn, row["id"]):
            access.seed_agent(conn, row["id"], owner)
        if conn.execute(f"SELECT 1 FROM access_grants WHERE agent_id = ? AND capability = ? AND {store.ACTIVE}",
                        (row["id"], cap, access.now_iso())).fetchone() is None:
            access._insert_grant(conn, row["id"], cap, owner, "platform", why)
            access.refresh_cache(conn, row["id"])
        done.append(key)
    if done:
        settings_store.put(conn, Ctx(owner, via="system"), SEED_STATE, sorted(done_before | set(done)))
        audit.log(conn, Ctx(owner, via="system"), "access_grant", None, None, source="platform", granted=done)
    conn.commit()
    return done
