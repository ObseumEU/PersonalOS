"""Single-user login backed by a signed session cookie."""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .config import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class Me(BaseModel):
    authenticated: bool
    login_required: bool


def require_user(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Dependency for every protected route."""
    if not settings.password:
        return
    if not request.session.get("user"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")


@router.post("/login")
def login(body: LoginRequest, request: Request, settings: Settings = Depends(get_settings)) -> Me:
    if settings.password and not hmac.compare_digest(
        body.password.encode(), settings.password.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    request.session["user"] = "owner"
    return Me(authenticated=True, login_required=bool(settings.password))


@router.post("/logout")
def logout(request: Request, settings: Settings = Depends(get_settings)) -> Me:
    request.session.clear()
    return Me(authenticated=not settings.password, login_required=bool(settings.password))


@router.get("/me")
def me(request: Request, settings: Settings = Depends(get_settings)) -> Me:
    authenticated = not settings.password or bool(request.session.get("user"))
    return Me(authenticated=authenticated, login_required=bool(settings.password))
