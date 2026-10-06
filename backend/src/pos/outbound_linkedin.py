"""LinkedIn on the owner's personal profile (request_outbound "linkedin.post"): always approval first.

The flow: an agent asks `request_outbound("linkedin.post", {text, image_file_id?, image_alt?})` → the guard
classifies it personal_channel (always, whatever the agent says) → the owner's approval card shows the final
text and the image → on approval `execute` publishes it with the official API (Posts API, `w_member_social`),
or, until LinkedIn is connected, marks it **připraveno k publikaci** ("ready_to_publish") and gives the owner
one item with the text ready to copy. A ready post is published later with one click
(POST /api/integrations/linkedin/publish/{approval_id}) once LinkedIn is connected.

Connecting: the owner never looks for a key. "Připojit LinkedIn" (POST /api/integrations/linkedin/agent-connect)
gives an agent with the browser (Content & Brand) one task: in its live browser it opens the LinkedIn developer
portal, creates or finds the PersonalOS app (products "Sign In with LinkedIn using OpenID Connect" and "Share on
LinkedIn", the redirect URL https://<POS_PUBLIC_URL>/api/integrations/linkedin/callback), takes the client id and
secret off the page with `browser_capture_secret` (into PersonalOS, encrypted; the model never sees them), and
opens the consent (`linkedin_connect(action="authorize_url")`). Wherever only the owner can act (the login, a 2FA
code, accepting LinkedIn's terms, "Allow") it hands him the prepared page (pos.handoff) and he just does that one
click. The callback accepts the agent's one-time state (no PersonalOS session in the agent's browser) or the
owner's own session state. POS_LINKEDIN_CLIENT_ID / POS_LINKEDIN_CLIENT_SECRET in .env still work and win.

Why the official API and not posting through the browser: LinkedIn's User Agreement forbids bots and automated
access to the site; the API with the owner's consent is the sanctioned way and does not break with every UI change.

The token and the app's secret are kept encrypted at rest (AES-GCM, a key derived from POS_SECRETS_KEY or the
session secret) in <data>/secrets/linkedin.bin and linkedin-app.bin, never shown, never logged. Access tokens last
~60 days: 7 days before the end the same agent flow runs again (the owner only clicks if LinkedIn asks).
"""

import contextlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import actors, audit, tasks
from .auth import require_user
from .core import Ctx, now_iso

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API = "https://api.linkedin.com"
SCOPES = "openid profile w_member_social"
_transport: httpx.BaseTransport | None = None  # tests put a mock transport here


def _version() -> str:
    return os.environ.get("POS_LINKEDIN_VERSION") or "202608"


def _settings():
    from .config import get_settings

    return get_settings()


def redirect_uri() -> str:
    base = (os.environ.get("POS_PUBLIC_URL") or "https://personalos.obseum.cz").rstrip("/")
    return f"{base}/api/integrations/linkedin/callback"


def app_creds() -> tuple[str, str]:
    """(client id, client secret): .env first, else what the agent captured in the developer portal."""
    if os.environ.get("POS_LINKEDIN_CLIENT_ID") and os.environ.get("POS_LINKEDIN_CLIENT_SECRET"):
        return os.environ["POS_LINKEDIN_CLIENT_ID"], os.environ["POS_LINKEDIN_CLIENT_SECRET"]
    app = _load("linkedin-app.bin", b"linkedin-app") or {}
    return str(app.get("client_id") or ""), str(app.get("client_secret") or "")


def app_configured() -> bool:
    cid, secret = app_creds()
    return bool(cid and secret)


def save_app(**values: str) -> None:
    """The app's client id / secret the agent took off the developer portal (browser_capture_secret)."""
    app = _load("linkedin-app.bin", b"linkedin-app") or {}
    app.update({k: v for k, v in values.items() if k in ("client_id", "client_secret") and v})
    _store("linkedin-app.bin", b"linkedin-app", app)


# ------------------------------------------------------------------ the token, encrypted at rest

def _path() -> Path:
    return Path(_settings().data_dir) / "secrets" / "linkedin.bin"


def _key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    master = (os.environ.get("POS_SECRETS_KEY") or _settings().session_secret or "").encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"pos-linkedin-v1", info=b"owner").derive(master)


def _store(name: str, aad: bytes, data: dict) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    blob = b"PLI1" + nonce + AESGCM(_key()).encrypt(nonce, json.dumps(data).encode(), aad)
    p = _path().with_name(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(p)


def _load(name: str, aad: bytes) -> dict | None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = _path().with_name(name).read_bytes()
    except OSError:
        return None
    if not blob.startswith(b"PLI1"):
        return None
    try:
        return json.loads(AESGCM(_key()).decrypt(blob[4:16], blob[16:], aad))
    except InvalidTag:
        return None


def save_token(data: dict) -> None:
    _store("linkedin.bin", b"linkedin", data)


def load_token() -> dict | None:
    if os.environ.get("POS_LINKEDIN_ACCESS_TOKEN") and os.environ.get("POS_LINKEDIN_AUTHOR"):
        return {"access_token": os.environ["POS_LINKEDIN_ACCESS_TOKEN"], "author": os.environ["POS_LINKEDIN_AUTHOR"],
                "expires_at": None, "name": "(env)"}
    return _load("linkedin.bin", b"linkedin")


def connected() -> dict | None:
    """The token when it is usable now, else None."""
    t = load_token()
    if not t:
        return None
    if t.get("expires_at") and t["expires_at"] < time.time() + 60:
        return None
    return t


def status(conn: sqlite3.Connection | None = None) -> dict:
    t = load_token()
    out = {"app": app_configured(), "connected": bool(connected()), "name": (t or {}).get("name"),
           "expires_at": (t or {}).get("expires_at"), "redirect_uri": redirect_uri(), "scopes": SCOPES}
    if conn is not None:
        task = open_connect_task(conn)
        out["flow"] = ({"task_ref": tasks.display_id(task["id"]), "status": task["status"],
                        "agent": task["agent_name"]} if task else None)
    return out


# ------------------------------------------------------------------ OAuth

def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": app_creds()[0],
                                       "redirect_uri": redirect_uri(), "state": state, "scope": SCOPES})


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=60)


def exchange(code: str) -> dict:
    with _client() as c:
        r = c.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                                    "client_id": app_creds()[0], "client_secret": app_creds()[1]})
        if r.status_code != 200:
            raise RuntimeError(f"LinkedIn refused the code ({r.status_code})")
        tok = r.json()
        me = c.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"})
        if me.status_code != 200:
            raise RuntimeError(f"LinkedIn userinfo → {me.status_code}")
        info = me.json()
    data = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
            "expires_at": time.time() + float(tok.get("expires_in") or 5_184_000),
            "author": f"urn:li:person:{info['sub']}", "name": info.get("name"), "scope": tok.get("scope")}
    save_token(data)
    return {"name": data["name"], "expires_at": data["expires_at"]}


# ------------------------------------------------------------------ publishing

_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])")
_HASHTAG = re.compile(r"(?<![\w#])#(\w[\w-]*)", re.UNICODE)


def commentary(text: str) -> str:
    """LinkedIn's "little text": reserved characters escaped, #tags as hashtag templates."""
    out, last = [], 0
    for m in _HASHTAG.finditer(text):
        out.append(_RESERVED.sub(r"\\\1", text[last:m.start()]))
        out.append("{hashtag|\\#|" + m.group(1) + "}")
        last = m.end()
    out.append(_RESERVED.sub(r"\\\1", text[last:]))
    return "".join(out)


def _headers(tok: dict) -> dict:
    return {"Authorization": f"Bearer {tok['access_token']}", "LinkedIn-Version": _version(),
            "X-Restli-Protocol-Version": "2.0.0"}


def _image_bytes(conn: sqlite3.Connection, file_id: int) -> tuple[bytes, str]:
    from . import files

    path, meta = files.content(conn, Ctx(actors.owner_id(conn), via="linkedin"), _settings().files_dir, int(file_id))
    return path.read_bytes(), meta.get("mime") or "image/jpeg"


def publish(conn: sqlite3.Connection, payload: dict) -> dict:
    tok = connected()
    if not tok:
        raise RuntimeError("LinkedIn is not connected")
    body = {"author": tok["author"], "commentary": commentary(str(payload["text"])), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    with _client() as c:
        if payload.get("image_file_id"):
            data, mime = _image_bytes(conn, payload["image_file_id"])
            init = c.post(f"{API}/rest/images?action=initializeUpload", headers=_headers(tok),
                          json={"initializeUploadRequest": {"owner": tok["author"]}})
            init.raise_for_status()
            v = init.json()["value"]
            up = c.put(v["uploadUrl"], content=data, headers={"Authorization": f"Bearer {tok['access_token']}",
                                                              "Content-Type": mime})
            up.raise_for_status()
            body["content"] = {"media": {"id": v["image"], "altText": str(payload.get("image_alt") or "")[:4000]}}
        r = c.post(f"{API}/rest/posts", headers=_headers(tok), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"LinkedIn posts → {r.status_code}: {r.text[:300]}")
    urn = r.headers.get("x-restli-id") or ""
    return {"status": "sent", "post": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else None}


def ready_item(conn: sqlite3.Connection, approval: dict, payload: dict) -> dict:
    """Approved but LinkedIn is not connected: the owner's one item with the text to copy."""
    owner = actors.owner_id(conn)
    text = str(payload.get("text") or "")
    first = " ".join(text.split())[:70]
    image = f"\n\nObrázek: /api/files/{payload['image_file_id']}/content" if payload.get("image_file_id") else ""
    t = tasks.create(conn, Ctx(owner, via="system"), {
        "title": f"LinkedIn: připraveno k publikaci – {first}"[:200],
        "notes": ("### Co udělat\nPříspěvek jsi schválil, LinkedIn ještě není připojený. Klikni na „Připojit "
                  "LinkedIn“ v Čeká na tebe: agent všechno připraví ve svém prohlížeči, ty se jen přihlásíš a "
                  f"potvrdíš. Pak ho publikuješ jedním klikem (schválení #{approval['id']}).\n\n"
                  f"### Text\n```\n{text}\n```{image}"),
        "definition_of_done": "Příspěvek je na LinkedInu (ručně nebo jedním klikem po připojení).",
        "priority": 2, "assignee": "me", "status": "next", "topic": "linkedin", "source": "outbound:linkedin"})
    return {"status": "ready_to_publish", "owner_task": t["ref"], "text": text}


def execute(conn: sqlite3.Connection, approval: dict, payload: dict) -> dict:
    if not connected():
        return ready_item(conn, approval, payload)
    try:
        return publish(conn, payload)
    except Exception as e:  # noqa: BLE001 - report it, keep the text for the owner
        return {**ready_item(conn, approval, payload), "error": str(e)[:300]}


def publish_ready(conn: sqlite3.Connection, ctx: Ctx, approval_id: int) -> dict:
    """The owner's one click: publish an approved post that waited (ready_to_publish)."""
    from . import approvals, outbound

    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise HTTPException(403, "only the owner publishes on his LinkedIn")
    a = approvals.get(conn, approval_id)
    if a["action"] != "linkedin.post" or a["status"] != "approved":
        raise HTTPException(400, "not an approved LinkedIn post")
    if (a.get("result") or {}).get("status") == "sent":
        return a["result"]
    payload = a["details"].get("payload") or {}
    result = publish(conn, payload)
    old = a.get("result") or {}
    conn.execute("UPDATE approvals SET result = ?, executed_at = ? WHERE id = ?",
                 (json.dumps(result, ensure_ascii=False), now_iso(), approval_id))
    audit.log(conn, ctx, "outbound:linkedin.post", "approval", approval_id, kind="personal_channel",
              summary=outbound.summarize("linkedin.post", payload), **result)
    if old.get("owner_task"):
        with contextlib.suppress(Exception):
            tasks.update(conn, ctx, tasks.parse_id(old["owner_task"]), {"status": "done"})
    conn.commit()
    return result


# ------------------------------------------------------------------ connecting through an agent

CONNECT_SOURCE = "linkedin:connect"
# Who does it: an agent with the browser that owns LinkedIn work, else the next one with the browser.
CONNECT_AGENTS = ("Content & Brand", "Head of Growth", "Chief of Staff", "CEO")
FLOW_TTL_S = 45 * 60
FLOW_KEY = "linkedin.flow_states"


def open_connect_task(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """SELECT t.id, t.status, t.assignee_id, a.name AS agent_name FROM tasks t
           LEFT JOIN actors a ON a.id = t.assignee_id
           WHERE t.source = ? AND t.status != 'done' AND t.archived_at IS NULL ORDER BY t.id DESC LIMIT 1""",
        (CONNECT_SOURCE,)).fetchone()
    return dict(row) if row else None


def _connect_agent(conn: sqlite3.Connection) -> sqlite3.Row:
    from . import browser

    for name in CONNECT_AGENTS:
        row = actors.find_by_name(conn, name)
        if row is not None and row["kind"] != "human" and browser.may_browse(conn, row["id"]):
            return row
    for row in conn.execute("SELECT * FROM actors WHERE kind != 'human' AND archived_at IS NULL ORDER BY id"):
        if browser.may_browse(conn, row["id"]):
            return row
    raise HTTPException(409, "no agent has the browser (tool:browser) to connect LinkedIn")


def connect_notes(reconnect: bool = False) -> str:
    """The agent's playbook. The owner only logs in and clicks what is his; he never looks for a key."""
    base = (os.environ.get("POS_PUBLIC_URL") or "https://personalos.obseum.cz").rstrip("/")
    what = ("The LinkedIn access expires soon: renew it" if reconnect
            else "Connect the owner's LinkedIn to PersonalOS")
    return f"""### Purpose
{what} (official API: the products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect"), so the \
posts he approves publish with one click. **The owner only logs in and clicks the buttons that are his** \
(LinkedIn's terms, "Allow"). Never ask him to find, copy or paste any key, ID or secret: you get everything in \
your browser.

### Steps
1. `linkedin_connect(action="status")`: what is missing and the exact redirect URL.
2. App credentials missing (`app: false`):
   a. `browser_navigate("https://www.linkedin.com/developers/apps")`. A login page: type nothing of his; call \
`browser_request_owner_handoff(title="Přihlas se do LinkedIn – zbytek udělám já", reason="...", \
done_url_contains="/developers/apps")` and wait.
   b. An app for PersonalOS exists: open it. Otherwise "Create app" and fill in everything: App name `PersonalOS`, \
LinkedIn Page: the company page he admins (if the list is empty or unclear, leave it for him), Privacy policy URL \
`{base}/`, App logo: `logo.png` from your work folder (get it first: \
`curl -sSfo logo.png http://web/icons/icon-512.png`, or `{base}/icons/icon-512.png`). Then hand over only the \
legal checkbox and "Create app": `browser_request_owner_handoff(title="Potvrď podmínky a vytvoř aplikaci", \
done_url_contains="/settings")`.
   c. Products tab: "Request access" for "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect". \
Each asks to accept terms: open the dialog, then hand it over ("Potvrď podmínky LinkedIn").
   d. Auth tab: add the redirect URL exactly as `status` gives it under "Authorized redirect URLs" and save. Then \
`browser_capture_secret(target="linkedin.client_id", ref=...)` and \
`browser_capture_secret(target="linkedin.client_secret", ref=..., reveal_ref=...)` (reveal_ref: the eye / show \
button when the secret is masked). They go straight into PersonalOS; never read the secret with \
browser_get_page_text, browser_read_page or a screenshot.
3. `linkedin_connect(action="authorize_url")`, then `browser_navigate` to that URL. A consent (or login) screen: \
`browser_request_owner_handoff(title="Klikni Povolit na LinkedIn", \
done_url_contains="/api/integrations/linkedin/callback")`.
4. `linkedin_connect(action="status")` must say `connected: true`; then `complete_task` with one line (whose \
profile). Stuck: hand the task back with the exact step and what the page said.

### Rules
- LinkedIn's terms: no posting, scraping or messaging through the browser; the browser is only for the developer \
portal and the consent. Posts go out through the API after the owner's approval.
- Ask the owner only through the handoff, with the page ready; keep the login ("Nechat přihlášení") so a renewal \
needs no login."""


def start_connect(conn: sqlite3.Connection, ctx: Ctx, reconnect: bool = False) -> dict:
    """The owner's "Připojit LinkedIn" (or the expiry check): one task for the agent with the browser."""
    task = open_connect_task(conn)
    if task:
        return {"task_ref": tasks.display_id(task["id"]), "agent": task["agent_name"], "existing": True}
    agent = _connect_agent(conn)
    title = ("LinkedIn: obnovit připojení (majitel jen potvrdí, když bude třeba)" if reconnect
             else "LinkedIn: připojit přes prohlížeč (majitel se jen přihlásí a potvrdí)")
    t = tasks.create(conn, ctx, {
        "title": title, "notes": connect_notes(reconnect), "priority": 1, "status": "next", "topic": "linkedin",
        "assignee": {"type": "agent", "id": agent["id"]}, "source": CONNECT_SOURCE,
        "definition_of_done": "linkedin_connect(action='status') says connected: true."})
    audit.log(conn, ctx, "linkedin_connect_started", "task", tasks.parse_id(t["ref"]), agent_id=agent["id"],
              reconnect=reconnect)
    conn.commit()
    from . import wake

    wake.wake(agent["id"])
    return {"task_ref": t["ref"], "agent": agent["name"], "existing": False}


def _flow_hash(state: str) -> str:
    import hashlib

    return hashlib.sha256(state.encode()).hexdigest()


def new_flow_state(conn: sqlite3.Connection, ctx: Ctx) -> str:
    """A one-time OAuth state for the agent's browser (it has no PersonalOS session). Only the hash is kept."""
    from . import settings_store

    now = time.time()
    states = {k: v for k, v in (settings_store.get(conn, FLOW_KEY) or {}).items() if v.get("exp", 0) > now}
    state = secrets.token_urlsafe(32)
    states[_flow_hash(state)] = {"exp": now + FLOW_TTL_S, "agent": ctx.actor_id}
    settings_store.put(conn, Ctx(actors.owner_id(conn), via="system"), FLOW_KEY, states)
    return state


def use_flow_state(conn: sqlite3.Connection, state: str) -> dict | None:
    """Spend a flow state (once): its record when it was issued and has not expired, else None."""
    from . import settings_store

    states = settings_store.get(conn, FLOW_KEY) or {}
    got = states.pop(_flow_hash(state or ""), None)
    if got is None:
        return None
    settings_store.put(conn, Ctx(actors.owner_id(conn), via="system"), FLOW_KEY, states)
    return got if got.get("exp", 0) > time.time() else None


def _connect_task_of(conn: sqlite3.Connection, agent_id: int) -> dict:
    task = open_connect_task(conn)
    if task is None or task["assignee_id"] != agent_id:
        raise PermissionError("no open LinkedIn connect task is yours (the owner starts it: Připojit LinkedIn)")
    return task


# What browser_capture_secret may store, and from which pages (pos.api_worker /browser/capture).
CAPTURE_TARGETS = {"linkedin.client_id": ("www.linkedin.com", "linkedin.com"),
                   "linkedin.client_secret": ("www.linkedin.com", "linkedin.com")}


def capture(conn: sqlite3.Connection, agent_id: int, target: str, value: str) -> int:
    """A value the agent's guard took off the developer portal (the model never saw it). Returns its length."""
    _connect_task_of(conn, agent_id)
    value = (value or "").strip()
    if not value or len(value) > 400 or set(value) <= set("•*·●. "):
        raise ValueError("no value on the page there (masked or empty): reveal it first (reveal_ref)")
    save_app(**{target.split(".", 1)[1]: value})
    return len(value)


def register(mcp, session) -> None:
    """The agent's side of connecting: linkedin_connect(action=status | authorize_url)."""
    from mcp.server.mcpserver import Context

    from . import mcp_server

    mcp_server.TOOL_PERMISSIONS.setdefault("linkedin_connect", "tasks:claim")

    @mcp.tool(name="linkedin_connect", description=(
        "Connecting the owner's LinkedIn (only with your open 'LinkedIn: připojit' task; its notes are the "
        "playbook). action='status': what is missing (app credentials, the consent) and the redirect URL to "
        "register in the developer app. action='authorize_url': the consent URL to open in your browser (one-time "
        "state, 45 min); LinkedIn sends the browser back to PersonalOS, which stores the token. You never see a key."))
    def linkedin_connect(ctx: Context, action: str = "status") -> dict:
        with session(ctx, "linkedin_connect", action=action) as (conn, c):
            try:
                task = _connect_task_of(conn, c.actor_id)
            except PermissionError as e:
                raise ValueError(str(e)) from None
            st = status()
            out = {"task": tasks.display_id(task["id"]), "app": st["app"], "connected": st["connected"],
                   "name": st["name"], "redirect_uri": st["redirect_uri"], "scopes": st["scopes"]}
            if action == "authorize_url":
                if not st["app"]:
                    raise ValueError("the app's client id and secret are not captured yet (step 2 of the task)")
                out["url"] = auth_url(new_flow_state(conn, c))
                conn.commit()
            return out


# ------------------------------------------------------------------ REST (the owner)

router = APIRouter(prefix="/api/integrations/linkedin", tags=["connectors"], dependencies=[Depends(require_user)])
# LinkedIn sends a browser back here: the owner's own (session state) or the agent's (a one-time flow state).
callback_router = APIRouter(prefix="/api/integrations/linkedin", tags=["connectors"])


def _owner(conn, ctx) -> None:
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise HTTPException(403, "only the owner connects his LinkedIn")


def _deps():
    from .api_tasks import get_ctx, get_db

    return get_db, get_ctx


_get_db, _get_ctx = _deps()


@router.get("/status")
def li_status(conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    _owner(conn, ctx)
    return status(conn)


@router.post("/agent-connect")
def li_agent_connect(conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    """"Připojit LinkedIn": an agent prepares everything in its browser; the owner only logs in and confirms."""
    _owner(conn, ctx)
    return start_connect(conn, ctx)


@router.get("/start")
def li_start(request: Request, conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    """The owner's own consent in his browser when the app is set up; otherwise the agent flow starts."""
    _owner(conn, ctx)
    if not app_configured():
        start_connect(conn, ctx)
        return RedirectResponse("/today?linkedin=agent", status_code=302)
    state = secrets.token_urlsafe(24)
    request.session["linkedin_state"] = state
    return RedirectResponse(auth_url(state), status_code=302)


_DONE_PAGE = ("<!doctype html><meta charset=utf-8><title>LinkedIn připojen</title>"
              "<body style='font:16px system-ui;margin:3em'><h1>LinkedIn je připojený</h1>"
              "<p>Schválené příspěvky teď jdou ven jedním klikem.</p></body>")


@callback_router.get("/callback")
def li_callback(request: Request, code: str = "", state: str = "", error: str = "", conn=Depends(_get_db)):
    """LinkedIn's redirect: the owner's own consent (the state in his session) or the agent's (a one-time
    flow state, its browser has no PersonalOS session). Anything else is refused."""
    from fastapi.responses import HTMLResponse

    from .auth import session_actor

    want = request.session.pop("linkedin_state", None)
    owner_flow = bool(want and state and secrets.compare_digest(want, state))
    flow = None if owner_flow else use_flow_state(conn, state)
    conn.commit()  # a flow state is spent even when what follows fails
    if owner_flow:
        aid = session_actor(request) or actors.owner_id(conn)
        if not actors.get(conn, aid)["is_owner"]:
            raise HTTPException(403, "only the owner connects his LinkedIn")
        ctx = Ctx(aid, via="api")
    elif flow:
        ctx = Ctx(int(flow.get("agent") or actors.owner_id(conn)), via="linkedin-flow")
    else:
        raise HTTPException(400, "LinkedIn consent failed (bad or used state)")
    if error or not code:
        raise HTTPException(400, f"LinkedIn consent failed ({error or 'no code'})")
    try:
        who = exchange(code)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    audit.log(conn, ctx, "linkedin_connected", None, None, name=who.get("name"),
              flow="owner" if owner_flow else "agent")
    conn.commit()
    if owner_flow:
        return RedirectResponse("/approvals?linkedin=connected", status_code=302)
    return HTMLResponse(_DONE_PAGE)


@router.post("/publish/{approval_id}")
def li_publish(approval_id: int, conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    try:
        return publish_ready(conn, ctx, approval_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


def expiry_check(conn: sqlite3.Connection) -> dict:
    """7 days before the token ends: the agent renews it in its browser (the owner clicks only if LinkedIn asks)."""
    t = load_token()
    if not t or not t.get("expires_at") or t["expires_at"] > time.time() + 7 * 86400:
        return {}
    if open_connect_task(conn):
        return {}
    try:
        out = start_connect(conn, Ctx(actors.owner_id(conn), via="system"), reconnect=True)
    except HTTPException:
        return {}
    return {"reconnect_task": out["task_ref"]}
