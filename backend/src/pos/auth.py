"""Login backed by a signed session cookie.

People sign in with their e-mail and password (pos.accounts); the session
carries their actor id. The single POS_PASSWORD login stays as the owner's
emergency account. Without POS_PASSWORD (local development only) there is no
login and every request acts as the owner.
"""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .config import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str
    email: str | None = None


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


@router.post("/login")
def login(body: LoginRequest, request: Request, settings: Settings = Depends(get_settings)) -> Me:
    from . import accounts
    from .db import connect

    if body.email:
        conn = connect(settings.db_path)
        try:
            actor_id = accounts.login(conn, body.email, body.password)
        except accounts.AuthError as e:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
        finally:
            conn.close()
        request.session.clear()
        request.session["actor_id"] = actor_id
        return _me(request, settings)
    if settings.password and not hmac.compare_digest(
        body.password.encode(), settings.password.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    request.session.clear()
    request.session["user"] = "owner"
    return _me(request, settings)


@router.post("/logout")
def logout(request: Request, settings: Settings = Depends(get_settings)) -> Me:
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
