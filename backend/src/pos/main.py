import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from . import __doc__ as description
from . import (a2a, actors, api_agents, api_chat, api_connectors, api_deploys, api_files, api_projects, api_tasks,
               api_tools, api_worker, chat, integrations, mcp_server, scheduler)
from .auth import require_user
from .auth import router as auth_router
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


def _claude_selfcheck(db_path) -> None:
    from . import engines

    conn = connect(db_path)
    try:
        engines.claude_selfcheck(conn)
    finally:
        conn.close()


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
            a2a.configure_builtin(conn)
            from . import workers

            workers.mark_services(conn)  # built-in automation (the Deployer) is a service, not an agent
            scheduler.seed(conn)
            chat.ensure_team_channel(conn)
            chat.ensure_system_channel(conn)  # automated notices, so #team stays for people
            from . import agents_code, projects

            projects.migrate_step_projects(conn)  # once: tasks with steps become projects
            agents_code.ensure_from_repo(conn, settings.data_dir)  # role agents from agents/*/agent.json
            from .access import service as access

            access.seed(conn)  # their permissions as grants too
            agents_code.grants_from_repo(conn)  # agent.json "grants" (e.g. tool:knowledge), once each
            from . import business

            business.ensure_schema(conn)  # tasks.value_kind
            business.reconcile_ledger(conn)  # runs.cost_usd derived from engine_usage (one ledger)
            from . import routing

            routing.ensure_business_rules(conn)  # mail leads → Growth, ObseumEU GitHub → CTO triage
            from . import browser as browser_use

            browser_use.ensure_grants(conn)  # tool:browser / tool:computer on day one (docs/BROWSER.md)
            from . import monitor

            monitor.ensure(conn)  # the Monitor agent's routing rule and budget (the sentinel's incidents)
            from . import observability

            observability.ensure(conn)  # Grafana alerts → the Monitor, and its ops:observe grant
            if os.environ.get("POS_WORKER_KEYS_DIR"):
                agents_code.write_worker_keys(conn, Path(os.environ["POS_WORKER_KEYS_DIR"]))
            if settings.scheduler:
                from . import weekly

                try:  # a weekly report the Friday job missed (W39) is written now, not next Friday
                    weekly.catch_up(conn)
                except Exception:  # noqa: BLE001 - never block the start on the report
                    logging.getLogger(__name__).exception("weekly report catch-up failed")
        finally:
            conn.close()
        integrations.install()
        hr_task = (asyncio.create_task(integrations.hr_loop(settings.db_path))
                   if hr_schedule.HRSettings().scheduler else None)
        sched_task = asyncio.create_task(scheduler.loop(settings.db_path)) if settings.scheduler else None
        from . import fastlane

        # A person's chat message to a busy agent gets a fast answer when its run is inside a long step.
        fast_task = (asyncio.create_task(fastlane.loop(settings.db_path))
                     if settings.scheduler and os.environ.get("POS_FASTLANE", "1") != "0" else None)
        if settings.scheduler and os.environ.get("POS_CLAUDE_SELFCHECK") == "1":
            asyncio.get_running_loop().run_in_executor(None, _claude_selfcheck, settings.db_path)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            for task in (hr_task, sched_task, fast_task):
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
    from .access.api import router as access_router

    app.include_router(access_router)
    from .credentials.api import router as credentials_router, worker as credentials_worker

    app.include_router(credentials_router)  # 1Password-backed credentials (pos.credentials)
    app.include_router(credentials_worker)
    app.include_router(hr_router)
    app.include_router(guard_api.router)
    guard_api.install_error_handler(app)
    app.include_router(api_tasks.router)
    app.include_router(api_projects.router)  # a project's page (pos.project_info)
    app.include_router(api_files.router)
    app.include_router(api_agents.router)
    app.include_router(api_worker.router)
    app.include_router(api_chat.router)
    app.include_router(api_connectors.router)
    app.include_router(api_connectors.hooks)
    app.include_router(api_connectors.machine)
    app.include_router(api_connectors.sentinel)
    app.include_router(a2a.router)
    app.include_router(api_deploys.router)
    app.include_router(api_tools.router)
    from . import api_reports

    app.include_router(api_reports.router)  # weekly reports and goals (pos.weekly, pos.goals)
    api_tasks.install_error_handlers(app)
    app.router.routes.extend(mcp_app.routes)

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/system", tags=["system"], dependencies=[Depends(require_user)])
    def system() -> dict[str, str]:
        from importlib.metadata import PackageNotFoundError, version

        try:
            v = version("personalos")
        except PackageNotFoundError:
            v = "dev"
        return {"version": v, "phase": "team: people and agents, projects, review, feedback, hiring"}

    return app


app = create_app()
