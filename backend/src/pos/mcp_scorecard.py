"""MCP tools of the company scorecard (pos.scorecard), kept out of mcp_server.py."""

TOOL_PERMISSIONS = {"scorecard": "tasks:read"}


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context

    from . import mcp_server
    from . import scorecard as sc

    for k, v in TOOL_PERMISSIONS.items():
        mcp_server.TOOL_PERMISSIONS.setdefault(k, v)

    @mcp.tool(description="The company scorecard, numbers from code (the Firma page): goals with baseline, current, "
                          "target and the week's change; what reached the world (outbound sent, replies, deploys, "
                          "customers helped); the owner's requests (delivered %, median hours, open); the business "
                          "share of spend (target >= 50 %) and the platform share (cap 30 %); cost per delivered "
                          "outcome; agent health (runs, review queue, loops, the owner's frustration); the top 3 "
                          "problems. format 'markdown' (default, Czech) or 'json'. Never estimate these numbers: "
                          "quote them from here.")
    def scorecard(ctx: Context, format: str = "markdown") -> dict:
        with session(ctx, "scorecard", format=format) as (conn, _):
            card = sc.view(conn)
            if format == "json":
                return card
            return {"day": card["day"], "markdown": sc.render(card), "problems": card["problems"]}

