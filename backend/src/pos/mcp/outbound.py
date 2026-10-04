"""MCP tools for outbound work: request_outbound (every send out of PersonalOS) and outbound_stats."""

from typing import Any

DESCRIPTION = (
    "Send something out of PersonalOS (constitution Ú1). Ordinary work goes out at once, audited, in the CEO's "
    "daily review: GitHub comments, issues, reviews and pull requests, Discord. E-MAIL becomes a Gmail DRAFT for "
    "the owner (his rule until he trusts it): it lands in the right mailbox and thread with his signature, he "
    "sends it from one 'Čeká na tebe' item; for your task that counts as done, never send it again. Money "
    "(payment, purchase, paid ads), commitments (contract, price quote, binding terms) and the owner's personal "
    "channels (his LinkedIn) go to his approval queue first and run once approved. "
    "action + payload: email.send {thread_id (a reply in that Gmail thread) | to + subject (a new message), "
    "body (no signature: it is added), cc?, account? (default the company mailbox; the personal one only for a "
    "personal task), language?, project? ('kniha' signs as Rodinné příběhy), campaign? (groups drafts in one "
    "item for the owner)}; github.comment {repo, number, body}; github.issue {repo, title, body, labels?}; "
    "github.review {repo, number, body, event?}; github.pr {repo, title, head, base?, body?, draft?}; "
    "discord.post {content}; linkedin.post {text, image_file_id?, image_alt?} (always approval); payment "
    "{to, amount, reason}; web.post {url, content}. The Kniha web is not published here: merge to its "
    "production branch (the kniha-deployer ships it). kind (optional): ordinary | money | commitment | "
    "personal_channel; it only ever moves a send toward approval. Idempotent: the same content to the same "
    "thread or target goes once (status duplicate). Limits per agent and day (status rate_limited). Returns "
    "status drafted | sent | duplicate | rate_limited | not_configured | failed, or the approval (sent: false).")


def register(mcp, session) -> None:
    from mcp.server.mcpserver import Context

    from .. import mcp_server, tasks

    mcp_server.TOOL_PERMISSIONS.setdefault("request_outbound", "approvals:request")
    mcp_server.TOOL_PERMISSIONS.setdefault("outbound_stats", "tasks:read")

    @mcp.tool(name="request_outbound", description=DESCRIPTION)
    def request_outbound(ctx: Context, action: str, payload: dict[str, Any], task_id: str | None = None,
                         why: str = "", kind: str | None = None) -> dict:
        from .. import outbound, taint

        with session(ctx, "request_outbound", action=action, task_id=task_id) as (conn, c):
            tid = tasks.parse_id(task_id) if task_id else None
            if tid:
                tasks.get(conn, c, tid)
            taint.check(conn, c, "request_outbound", {"action": action, "payload": payload, "kind": kind})
            return outbound.request(conn, c, action, payload, tid, why=why, kind=kind)

    @mcp.tool(name="outbound_stats", description=(
        "Outbound over the last `days` (default 7): sent per day, per agent, per kind and action, replies "
        "received in the threads we wrote to, what did not go out (failed, rate limited, duplicates), e-mail "
        "drafts waiting for the owner and his trust in them (sent unchanged / edited / discarded)."))
    def outbound_stats(ctx: Context, days: int = 7) -> dict:
        from .. import outbound

        with session(ctx, "outbound_stats", days=days) as (conn, _c):
            return outbound.outbound_stats(conn, max(1, min(int(days), 90)))
