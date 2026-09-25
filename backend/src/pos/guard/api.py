"""HTTP API for the constitution and guardrails, under /api/guard.

The core mounts it with ``app.include_router(guard.api.router)`` and maps
ConstitutionViolation to 403 with ``install_error_handler(app)``.
"""

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import commands, external, prompt
from .rules import RULES, ConstitutionViolation

router = APIRouter(prefix="/api/guard", tags=["guard"])


class ConstitutionOut(BaseModel):
    text: str
    sha256: str
    rules: dict[str, str]


class CommandIn(BaseModel):
    command: str
    external: bool = False


class DecisionOut(BaseModel):
    outcome: str
    rule: str | None
    reason: str


class WrapIn(BaseModel):
    source: str
    content: str
    ref: str | None = None


class WrapOut(BaseModel):
    wrapped: str
    signals: list[str]


@router.get("/constitution")
def get_constitution() -> ConstitutionOut:
    return ConstitutionOut(
        text=prompt.constitution_text(),
        sha256=prompt.constitution_digest(),
        rules={r.id: r.title for r in RULES.values()},
    )


@router.get("/agent-guardrails")
def get_agent_guardrails() -> dict[str, str]:
    """Text the Codex agent template prepends to every agent's instructions."""
    return {"text": prompt.agent_guardrails(), "sha256": prompt.constitution_digest()}


@router.post("/check-command")
def check_command(body: CommandIn) -> DecisionOut:
    """Dry-run classification for an agent (agent identity comes from the core)."""
    from .rules import SimpleActor

    trigger = commands.Trigger.EXTERNAL if body.external else commands.Trigger.MEMBER
    d = commands.evaluate(body.command, SimpleActor("agent"), trigger)
    return DecisionOut(outcome=d.outcome.value, rule=d.rule, reason=d.reason)


@router.post("/wrap")
def wrap(body: WrapIn) -> WrapOut:
    return WrapOut(
        wrapped=external.wrap_external(body.source, body.content, ref=body.ref),
        signals=external.scan(body.content).signals,
    )


def install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(ConstitutionViolation)
    async def _violation(_: Request, exc: ConstitutionViolation) -> JSONResponse:
        d = exc.decision
        return JSONResponse(
            status_code=403,
            content={
                "detail": d.reason,
                "rule": d.rule,
                "outcome": d.outcome.value,
                "details": d.details,
            },
        )
