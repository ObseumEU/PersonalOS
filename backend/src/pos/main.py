import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from . import __doc__ as description
from . import actors, api_agents, api_tasks, api_worker, integrations, mcp_server
from .auth import require_user
from .auth import router as auth_router
from .budget import service as budget_service
from .budget.api import router as budget_router
from .config import Settings, get_settings
from .db import connect, init_db
from .guard import api as guard_api
from .hr import schedule as hr_schedule
from .hr.api import router as hr_router


class MCPAuth:
    """Rejects /mcp requests without a valid bearer key before they reach the
    MCP transport. Tools resolve the actor from the same key again."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            key = mcp_server._bearer(headers)
            conn = connect(self.settings.db_path)
            try:
                actor = actors.actor_for_key(conn, key) if key else None
            finally:
                conn.close()
            if actor is None:
                await JSONResponse({"detail": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.password and settings.session_secret == "dev-only-change-me":
        raise RuntimeError("Set POS_SESSION_SECRET when POS_PASSWORD is set")

    mcp = mcp_server.build(settings.db_path)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Access is controlled by bearer keys; the server sits behind a proxy
        # with its own host name, so host-header pinning is off.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.files_dir.mkdir(parents=True, exist_ok=True)
        init_db(settings.db_path)
        conn = connect(settings.db_path)
        try:
            actors.ensure_builtin(conn)
            if settings.mcp_token:
                actors.ensure_key(conn, actors.owner_id(conn), settings.mcp_token, "POS_MCP_TOKEN")
            integrations.register_builtin_agents(conn)
        finally:
            conn.close()
        integrations.install()
        check_minutes = budget_service.BudgetSettings().check_minutes
        budget_task = (asyncio.create_task(integrations.budget_loop(settings.db_path, check_minutes))
                       if check_minutes > 0 else None)
        hr_task = (asyncio.create_task(integrations.hr_loop(settings.db_path))
                   if hr_schedule.HRSettings().scheduler else None)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            for task in (budget_task, hr_task):
                if task:
                    task.cancel()

    app = FastAPI(title="PersonalOS", description=description, lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: settings
    app.add_middleware(MCPAuth, settings=settings)
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
    app.include_router(hr_router)
    app.include_router(guard_api.router)
    guard_api.install_error_handler(app)
    app.include_router(api_tasks.router)
    app.include_router(api_agents.router)
    app.include_router(api_worker.router)
    api_tasks.install_error_handlers(app)
    app.router.routes.extend(mcp_app.routes)

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/system", tags=["system"], dependencies=[Depends(require_user)])
    def system() -> dict[str, str]:
        return {"version": "0.2.0", "phase": "step 1: tasks core and MCP"}

    return app


app = create_app()
