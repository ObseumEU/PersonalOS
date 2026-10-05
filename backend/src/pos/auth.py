"""Login backed by a signed session cookie.

People sign in with their e-mail and password (pos.accounts); the session
carries their actor id. The single POS_PASSWORD login stays as the owner's
emergency account. Without POS_PASSWORD (local development only) there is no
login and every request acts as the owner.
"""

import hmac
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .config import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str
    email: str | None = None
    # The installed app (/m): the device stays signed in for a year of use, not 30 days (pos.devices).
    app: bool = False


class AcceptRequest(BaseModel):
    token: str
    password: str


class PasswordRequest(BaseModel):
    old: str
    new: str


class Me(BaseModel):
    authenticated: bool
    login_required: bool
    actor_id: int | None = None
    name: str | None = None
    is_owner: bool = False


def session_actor(request: Request) -> int | None:
    """The signed-in person's actor id (None: the owner's emergency login or no login)."""
    value = request.session.get("actor_id")
    return int(value) if value else None


def require_user(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Dependency for every protected route."""
    if not settings.password:
        return
    if not request.session.get("user") and not request.session.get("actor_id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")


def _me(request: Request, settings: Settings) -> Me:
    from . import actors
    from .db import connect

    authenticated = not settings.password or bool(request.session.get("user") or request.session.get("actor_id"))
    if not authenticated:
        return Me(authenticated=False, login_required=True)
    conn = connect(settings.db_path)
    try:
        aid = session_actor(request) or actors.owner_id(conn)
        row = actors.get(conn, aid)
        return Me(authenticated=True, login_required=bool(settings.password), actor_id=aid, name=row["name"],
                  is_owner=bool(row["is_owner"]))
    finally:
        conn.close()


class LoginLimiter:
    """Password guessing is slowed per client IP: LIMIT failed logins within WINDOW_S, then
    the IP waits (429 + Retry-After) BACKOFF_S, doubling with every lock-out in a row up to
    BACKOFF_MAX_S. A successful login clears the IP. In memory: the API is one process."""

    LIMIT = 10
    WINDOW_S = 60.0
    BACKOFF_S = 60.0
    BACKOFF_MAX_S = 15 * 60.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    def retry_after(self, key: str) -> int:
        """Seconds the key still has to wait (0: it may try)."""
        with self._lock:
            left = self._until.get(key, 0.0) - self.clock()
            return int(left) + 1 if left > 0 else 0

    def failed(self, key: str) -> None:
        with self._lock:
            now = self.clock()
            fails = [t for t in self._fails.get(key, []) if now - t < self.WINDOW_S] + [now]
            if len(fails) >= self.LIMIT:
                strikes = self._strikes.get(key, 0) + 1
                self._strikes[key] = strikes
                self._until[key] = now + min(self.BACKOFF_S * 2 ** (strikes - 1), self.BACKOFF_MAX_S)
                fails = []
            self._fails[key] = fails
            if len(self._fails) > 10_000:  # a flood of addresses: forget the quiet ones
                for k in [k for k, v in self._fails.items() if not v or now - v[-1] >= self.WINDOW_S]:
                    self._fails.pop(k, None)

    def succeeded(self, key: str) -> None:
        with self._lock:
            for d in (self._fails, self._until, self._strikes):
                d.pop(key, None)


login_limiter = LoginLimiter()


def client_ip(request: Request) -> str:
    """The caller's address: the first X-Forwarded-For entry (the front proxy, Caddy, sets it
    to the real client; nginx appends), else the direct peer."""
    fwd = request.headers.get("x-forwarded-for", "")
    first = fwd.split(",")[0].strip()
    return first or (request.client.host if request.client else "?")


def _check_rate(key: str) -> None:
    wait = login_limiter.retry_after(key)
    if wait:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts; try again later",
                            headers={"Retry-After": str(wait)})


@router.post("/login")
def login(body: LoginRequest, request: Request, settings: Settings = Depends(get_settings)) -> Me:
    from . import accounts
    from .db import connect

    ip = client_ip(request)
    _check_rate(ip)
    if body.email:
        conn = connect(settings.db_path)
        try:
            actor_id = accounts.login(conn, body.email, body.password)
        except accounts.AuthError as e:
            login_limiter.failed(ip)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
        finally:
            conn.close()
        login_limiter.succeeded(ip)
        request.session.clear()
        request.session["actor_id"] = actor_id
        _new_device(request, settings, actor_id, owner_login=False, app=body.app)
        return _me(request, settings)
    if settings.password and not hmac.compare_digest(
        body.password.encode(), settings.password.encode()
    ):
        login_limiter.failed(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    login_limiter.succeeded(ip)
    request.session.clear()
    request.session["user"] = "owner"
    _new_device(request, settings, None, owner_login=True, app=body.app)
    return _me(request, settings)


def _new_device(request: Request, settings: Settings, actor_id: int | None, *, owner_login: bool, app: bool) -> None:
    """Every login is a device (pos.devices): listed in Nastavení, revocable."""
    from . import actors, devices
    from .db import connect

    if not settings.password:
        return
    conn = connect(settings.db_path)
    try:
        sid = devices.create(conn, actor_id or actors.owner_id(conn), owner_login=owner_login,
                             user_agent=request.headers.get("user-agent", ""), app=app)
        conn.commit()
    finally:
        conn.close()
    request.session["sid"] = sid


def _me_id(request: Request, settings: Settings) -> int:
    from . import actors
    from .db import connect

    aid = session_actor(request)
    if aid:
        return aid
    conn = connect(settings.db_path)
    try:
        return actors.owner_id(conn)
    finally:
        conn.close()


@router.post("/logout")
def logout(request: Request, settings: Settings = Depends(get_settings)) -> Me:
    sid = request.session.get("sid")
    if sid:  # signing out ends this device: its session and its push subscriptions
        from . import devices
        from .db import connect

        conn = connect(settings.db_path)
        try:
            devices.revoke(conn, _me_id(request, settings), sid)
            conn.commit()
        finally:
            conn.close()
    request.session.clear()
    return Me(authenticated=not settings.password, login_required=bool(settings.password))


@router.get("/me")
def me(request: Request, settings: Settings = Depends(get_settings)) -> Me:
    return _me(request, settings)


@router.get("/invite/{token}")
def invite_info(token: str, settings: Settings = Depends(get_settings)) -> dict:
    """Who an invitation is for (the invite page shows it)."""
    from . import accounts
    from .db import connect

    conn = connect(settings.db_path)
    try:
        return accounts.peek(conn, token)
    except accounts.AuthError as e:
        raise HTTPException(404, str(e)) from e
    finally:
        conn.close()


@router.post("/accept")
def accept(body: AcceptRequest, request: Request, settings: Settings = Depends(get_settings)) -> Me:
    """Accept an invitation: set a password, get an account, be signed in."""
    from . import accounts
    from .db import connect

    conn = connect(settings.db_path)
    try:
        actor_id = accounts.accept(conn, body.token, body.password)
    except accounts.AuthError as e:
        raise HTTPException(422, str(e)) from e
    finally:
        conn.close()
    request.session.clear()
    request.session["actor_id"] = actor_id
    _new_device(request, settings, actor_id, owner_login=False, app=False)
    return _me(request, settings)


@router.post("/password")
def change_password(body: PasswordRequest, request: Request, settings: Settings = Depends(get_settings)) -> dict:
    from . import accounts
    from .db import connect

    aid = session_actor(request)
    if aid is None:
        raise HTTPException(400, "sign in with your e-mail to change your password")
    conn = connect(settings.db_path)
    try:
        accounts.set_password(conn, aid, body.old, body.new)
    except accounts.AuthError as e:
        raise HTTPException(422, str(e)) from e
    finally:
        conn.close()
    return {"ok": True}


# ------------------------------------------------------------------ devices (pos.devices)

@router.get("/devices", dependencies=[Depends(require_user)])
def device_list(request: Request, settings: Settings = Depends(get_settings)) -> list[dict]:
    """The signed-in devices of this member; `current` is the one asking."""
    from . import devices
    from .db import connect

    conn = connect(settings.db_path)
    try:
        return devices.list_for(conn, _me_id(request, settings), request.session.get("sid"))
    finally:
        conn.close()


@router.post("/devices/revoke-others", dependencies=[Depends(require_user)])
def device_revoke_others(request: Request, label: str | None = None,
                         settings: Settings = Depends(get_settings)) -> dict:
    """"Odhlásit všechna ostatní": every device of this member except the one asking (only those with
    this label, for a group like "Neznámé zařízení ×234")."""
    from . import devices
    from .db import connect

    conn = connect(settings.db_path)
    try:
        n = devices.revoke_others(conn, _me_id(request, settings), request.session.get("sid"), label=label)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "revoked": n}


@router.post("/devices/{sid}/revoke", dependencies=[Depends(require_user)])
def device_revoke(sid: str, request: Request, settings: Settings = Depends(get_settings)) -> dict:
    from . import devices
    from .db import connect

    conn = connect(settings.db_path)
    try:
        if not devices.revoke(conn, _me_id(request, settings), sid):
            raise HTTPException(404, "no such device")
        conn.commit()
    finally:
        conn.close()
    if sid == request.session.get("sid"):
        request.session.clear()
    return {"ok": True}


@router.post("/devices/current/app", dependencies=[Depends(require_user)])
def device_is_app(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """The installed app runs on this device: keep it signed in for the app's lifetime (a year of use)."""
    from . import devices
    from .db import connect

    sid = request.session.get("sid")
    if not sid:
        return {"ok": False}
    conn = connect(settings.db_path)
    try:
        devices.ensure_schema(conn)
        devices.set_app(conn, sid)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
