# Access and budgets (the Access manager)

Code: `backend/src/pos/access/`. Agent: `agents/access-manager/` (Správce přístupů).

## Model
- **Grants** (`access_grants`): one row per capability per agent, with
  `granted_by`, `source` (seed, platform, owner, access_manager), `reason`,
  `created_at` and an optional `expires_at`. Capabilities: a permission group
  (`tasks:write`, …), one pos tool (`tool:<name>`), `outbound:<action>` or
  `outbound:*`, `scope:repo:<owner/name>`, `scope:connector:<name>`; the worker's own
  servers `tool:browser` and `tool:computer`, and the owner-only `scope:browser:<host>`
  (submitting there needs no approval) and `scope:browser-profile:<name>` ([BROWSER.md](BROWSER.md)).
- **Budgets** (`access_budgets`): `usd_day`, `usd_month`, `tokens_day`,
  `tokens_month`, `usd_run`, `runs_day` per agent; `agent_id` NULL is the
  company-wide cap. A temporary row sits on top of the permanent one and
  reverts when it expires.
- **Requests** (`access_requests`): what agents ask for (`request_access`) and
  what the platform raises for them (a limit hit, a spend spike).

Tables are created on first use (no numbered migration). Day one seeds the
grants from `actors.permissions` (+ `outbound:*` where the agent could request
approvals); `actors.permissions` stays as a mirror of the active grants.

## Enforcement (code)
- MCP: `agents.has_permission` reads the active grants; `tool:<name>` opens one tool.
- `request_outbound` needs an outbound grant; ordinary sends go out at once
  (audited), money, commitments and the owner's personal channels wait for
  approval (constitution Ú1).
- Every run (`start_run` and internal runs) passes `budget_gate`: the company
  cap first, then the agent's limits. `usd_run` is returned by `start_run`;
  the worker uses the tighter of it and `WORKER_CLAUDE_MAX_USD`.
- The kill switch is unchanged (`pos.killswitch`).

## The Access manager's hard limits
It decides alone (any capability, any budget, permanent or temporary) except:
nothing for itself, never the company cap, and never owner-only items
(`guard:*`, `constitution:*`, `secrets:*`, `credentials:*`, `cred:*` (one
1Password credential, [CREDENTIALS.md](CREDENTIALS.md)), `access:manage`,
`tool:access_*`). Every decision is audit-logged with its reason and posted in
#team.

## What the owner gets
- One daily digest (18:00): grants, revokes, raises, spend vs the company cap.
- An immediate DM only when the company cap is 80 % used (or full) or an agent
  was paused for a spike (last hour > 5× its hourly baseline and above a floor).
- Owner-only settings on the Access manager's page: company cap, spike
  settings; its own tools and budget on its agent page.

## Jobs
`access_expire` (5 min), `access_watch` (15 min), `access_digest` (daily 18:00),
`access_weekly` (Mon 07:30: a review task for the Access manager).

## LiteLLM
`access/litellm.py` creates/updates per-agent virtual keys (alias
`pos-agent-<id>`) and reads spend over the admin API. Off unless
`POS_LITELLM_URL` and `POS_LITELLM_ADMIN_KEY` are set; nothing calls it
automatically yet.
