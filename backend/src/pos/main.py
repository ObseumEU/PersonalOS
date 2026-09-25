from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.middleware.sessions import SessionMiddleware

from . import __doc__ as description
from .auth import require_user
from .auth import router as auth_router
from .budget.api import router as budget_router
from .config import Settings, get_settings
from .db import init_db


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.password and settings.session_secret == "dev-only-change-me":
        raise RuntimeError("Set POS_SESSION_SECRET when POS_PASSWORD is set")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.files_dir.mkdir(parents=True, exist_ok=True)
        init_db(settings.db_path)
        yield

    app = FastAPI(title="PersonalOS", description=description, lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="pos_session",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.include_router(auth_router)
    app.include_router(budget_router)

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/system", tags=["system"], dependencies=[Depends(require_user)])
    def system() -> dict[str, str]:
        return {"version": "0.1.0", "phase": "1"}

    return app


app = create_app()
