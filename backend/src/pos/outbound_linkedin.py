"""LinkedIn on the owner's personal profile (request_outbound "linkedin.post"): always approval first.

The flow: an agent asks `request_outbound("linkedin.post", {text, image_file_id?, image_alt?})` → the guard
classifies it personal_channel (always, whatever the agent says) → the owner's approval card shows the final
text and the image → on approval `execute` publishes it with the official API (Posts API, `w_member_social`),
or, until LinkedIn is connected, marks it **připraveno k publikaci** ("ready_to_publish") and gives the owner
one item with the text ready to copy. A ready post is published later with one click
(POST /api/integrations/linkedin/publish/{approval_id}) once LinkedIn is connected.

Connecting (the owner, once): a LinkedIn developer app with the products "Sign In with LinkedIn using OpenID
Connect" and "Share on LinkedIn", redirect URL https://personalos.obseum.cz/api/integrations/linkedin/callback;
POS_LINKEDIN_CLIENT_ID / POS_LINKEDIN_CLIENT_SECRET in .env; then GET /api/integrations/linkedin/start in his
browser (the consent). The token is kept encrypted at rest (AES-GCM, a key derived from the session secret)
in <data>/secrets/linkedin.bin, never shown, never logged. Access tokens last ~60 days: 7 days before the
end the owner gets one item to reconnect.
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


def app_configured() -> bool:
    return bool(os.environ.get("POS_LINKEDIN_CLIENT_ID") and os.environ.get("POS_LINKEDIN_CLIENT_SECRET"))


# ------------------------------------------------------------------ the token, encrypted at rest

def _path() -> Path:
    return Path(_settings().data_dir) / "secrets" / "linkedin.bin"


def _key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    master = (os.environ.get("POS_SECRETS_KEY") or _settings().session_secret or "").encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"pos-linkedin-v1", info=b"owner").derive(master)


def save_token(data: dict) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    blob = b"PLI1" + nonce + AESGCM(_key()).encrypt(nonce, json.dumps(data).encode(), b"linkedin")
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(p)


def load_token() -> dict | None:
    if os.environ.get("POS_LINKEDIN_ACCESS_TOKEN") and os.environ.get("POS_LINKEDIN_AUTHOR"):
        return {"access_token": os.environ["POS_LINKEDIN_ACCESS_TOKEN"], "author": os.environ["POS_LINKEDIN_AUTHOR"],
                "expires_at": None, "name": "(env)"}
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = _path().read_bytes()
    except OSError:
        return None
    if not blob.startswith(b"PLI1"):
        return None
    try:
        return json.loads(AESGCM(_key()).decrypt(blob[4:16], blob[16:], b"linkedin"))
    except InvalidTag:
        return None


def connected() -> dict | None:
    """The token when it is usable now, else None."""
    t = load_token()
    if not t:
        return None
    if t.get("expires_at") and t["expires_at"] < time.time() + 60:
        return None
    return t


def status() -> dict:
    t = load_token()
    return {"app": app_configured(), "connected": bool(connected()), "name": (t or {}).get("name"),
            "expires_at": (t or {}).get("expires_at"), "redirect_uri": redirect_uri(), "scopes": SCOPES}


# ------------------------------------------------------------------ OAuth

def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": os.environ["POS_LINKEDIN_CLIENT_ID"],
                                       "redirect_uri": redirect_uri(), "state": state, "scope": SCOPES})


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=60)


def exchange(code: str) -> dict:
    with _client() as c:
        r = c.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                                    "client_id": os.environ["POS_LINKEDIN_CLIENT_ID"],
                                    "client_secret": os.environ["POS_LINKEDIN_CLIENT_SECRET"]})
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
        "notes": ("### Co udělat\nPříspěvek jsi schválil, LinkedIn ještě není připojený. Zkopíruj text a publikuj "
                  "ho na svém profilu, nebo připoj LinkedIn (Nastavení → Konektory) a publikuj jedním klikem "
                  f"ve schvalování (#{approval['id']}).\n\n### Text\n```\n{text}\n```{image}"),
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


# ------------------------------------------------------------------ REST (the owner)

router = APIRouter(prefix="/api/integrations/linkedin", tags=["connectors"], dependencies=[Depends(require_user)])


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
    return status()


@router.get("/start")
def li_start(request: Request, conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    _owner(conn, ctx)
    if not app_configured():
        raise HTTPException(400, "POS_LINKEDIN_CLIENT_ID / POS_LINKEDIN_CLIENT_SECRET are not set (docs/CONNECTORS.md)")
    state = secrets.token_urlsafe(24)
    request.session["linkedin_state"] = state
    return RedirectResponse(auth_url(state), status_code=302)


@router.get("/callback")
def li_callback(request: Request, code: str = "", state: str = "", error: str = "",
                conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    _owner(conn, ctx)
    want = request.session.pop("linkedin_state", None)
    if error or not code or not want or not secrets.compare_digest(want, state):
        raise HTTPException(400, f"LinkedIn consent failed ({error or 'bad state'})")
    try:
        who = exchange(code)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    audit.log(conn, ctx, "linkedin_connected", None, None, name=who.get("name"))
    conn.commit()
    return RedirectResponse("/approvals?linkedin=connected", status_code=302)


@router.post("/publish/{approval_id}")
def li_publish(approval_id: int, conn=Depends(_get_db), ctx=Depends(_get_ctx)):
    try:
        return publish_ready(conn, ctx, approval_id)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


def expiry_check(conn: sqlite3.Connection) -> dict:
    """7 days before the token ends: one item for the owner to reconnect (deduplicated)."""
    t = load_token()
    if not t or not t.get("expires_at") or t["expires_at"] > time.time() + 7 * 86400:
        return {}
    open_ = conn.execute("""SELECT id FROM tasks WHERE source = 'outbound:linkedin-reconnect' AND status != 'done'
                            AND archived_at IS NULL""").fetchone()
    if open_:
        return {}
    owner = actors.owner_id(conn)
    tasks.create(conn, Ctx(owner, via="system"), {
        "title": "LinkedIn: znovu připojit (přístup brzy vyprší)", "priority": 2, "assignee": "me", "status": "next",
        "source": "outbound:linkedin-reconnect", "topic": "linkedin",
        "notes": "Otevři /api/integrations/linkedin/start a potvrď souhlas; jinak schválené posty nepůjdou ven.",
        "definition_of_done": "LinkedIn je znovu připojený."})
    conn.commit()
    return {"reconnect_item": True}
