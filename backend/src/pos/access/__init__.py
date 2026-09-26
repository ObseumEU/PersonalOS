"""Access and budgets: what each agent may use and spend (the Access manager).

Every agent's permissions live as **grants**: one row per capability, with who
granted it, why, when, and an optional expiry (a temporary grant reverts on
its own). Budgets are rows too (daily/monthly USD and tokens, USD per run,
runs per day), plus a company-wide cap. Enforcement is in code:

- the `pos` MCP server asks `pos.agents.has_permission`, which reads the
  active grants (a tool group like `tasks:write`, or one tool as `tool:<name>`);
- `start_run` (and every internal run) passes the budget gate here: the
  company cap first, then the agent's own limits. A refused run becomes a
  request for the Access manager;
- `request_outbound` needs an `outbound:<action>` grant, and every outbound
  action still waits for the owner's approval.

The **Access manager** (Správce přístupů) is the only non-owner actor with the
grant tools (`access:manage`). It decides on its own; the hard limits are here,
not in its prompt: it never grants or raises anything for itself, the company
cap and the kill switch are the owner's, and guard/constitution, secrets and
credentials and the grant tools themselves are owner-only. Every decision is
audit-logged with its reason and posted briefly in #team; the owner gets one
daily digest, plus an immediate ping only when the company cap is 80 % used or
an agent was paused for a spend spike.

Modules:
    store    tables (created on first use, no numbered migration)
    service  grants, budgets, requests, decisions, the run gate, the watchers
    litellm  the seam for per-agent LiteLLM virtual keys (off unless configured)
    mcp      the MCP tools (request_access for everyone, access_* for the manager)
    api      the web app's endpoints under /api/access
"""
