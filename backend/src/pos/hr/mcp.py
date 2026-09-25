"""HR tools on the `pos` MCP server, so agents (and Codex) reach HR over MCP."""

from typing import Literal

from mcp.server.mcpserver import Context

from . import service


def register(mcp, session) -> None:
    """Called by pos.mcp_server.build with its authenticated `session` helper."""

    @mcp.tool(description="HR: the agent roster with effectiveness scores (0-1) and what HR would change now. "
                          "Read-only.")
    def hr_overview(ctx: Context) -> dict:
        with session(ctx, "hr_overview") as (conn, _):
            return service.agents_overview(conn)

    @mcp.tool(description="HR: run the daily review. apply=true archives idle/finished/ineffective agents and "
                          "files merge and instruction tasks (owner or HR agent only); apply=false only reports.")
    def hr_review(ctx: Context, apply: bool = False) -> dict:
        with session(ctx, "hr_review", apply=apply) as (conn, c):
            return service.daily_review(conn, c, apply=apply)

    @mcp.tool(description="HR: check whether you may create a new agent under the limits (max active agents, "
                          "2 new agents per creator per day). Call before create_agent. Returns allowed, or HR's "
                          "decision: reuse an existing agent, replace, defer, or ask_owner.")
    def hr_admit_agent(ctx: Context, name: str, purpose: str,
                       lifetime: Literal["one_shot", "long_lived"] = "one_shot") -> dict:
        with session(ctx, "hr_admit_agent", name=name) as (conn, c):
            return service.admit_agent(conn, c, name=name, purpose=purpose, lifetime=lifetime)

    @mcp.tool(description="HR: file the weekly overview for the owner as a task to read (owner or HR agent only).")
    def hr_weekly_report(ctx: Context) -> dict:
        with session(ctx, "hr_weekly_report") as (conn, c):
            return service.weekly_report(conn, c)
