# Connectors (step 4)

Three kinds of traffic, each with one owner:

| Direction | Who does it | How |
|---|---|---|
| **Incoming events** → tasks | PersonalOS core (`pos.routing`) | Connector agents call MCP `emit_event`; GitHub calls `/api/hooks/github`; you can post a test event on the Connectors screen. |
| **Knowledge** (e-mails, threads, repos into the knowledge base) | knowlage-agent | knowlage ingests e-mail with its own Gmail connector and syncs GitHub itself. Agents may still push other material with their own KB key (`/ingest/mcp`), but the Mail agent does not. PersonalOS does not copy data. See `apps/knowlage-agent/docs/INGEST.md`. |
| **New mail → the Mail agent** | PersonalOS core (`pos.routing`, `pos.mailfilter`) | One `gmail` event per new message or thread (`POST /api/events`, or `emit_event`). A rule-based prefilter (no model) drops mailing lists (List-Unsubscribe, List-Id), Precedence bulk/list, no-reply/notification senders, Gmail's Promotions/Social/Updates/Forums and auto-submitted mail; they are stored and counted (Connectors), never run. Rules: `POS_MAIL_PREFILTER` (a JSON file, or `off`). The rest becomes a task for the Mail agent, which only reacts; replies go through the approval queue. |
| **Outbound actions** (e-mail, GitHub comment, Discord post) | PersonalOS core (`pos.outbound`) | Agents call MCP `request_outbound`; it runs only after you approve it in Approvals (constitution U1). |

## Routing rules

Rules are data (screen **Connectors**, API `/api/routes`, MCP `list_routes`),
versioned like tasks, first match wins. A rule matches on source, `kind`,
`label`, `from_contains` or `text_regex` and sends the event to an assignee
with a priority and topic. No match: the event lands in your inbox.

Defaults:

- GitHub issue labelled `agent` → Dev agent
- GitHub review request → Dev agent
- e-mail about an invoice (faktura / invoice / rechnung) → Nexus, P1 (payment needs your approval)
- any other e-mail → Mail agent (triage)
- Discord mention or question → Community agent

Event content is always stored wrapped as untrusted outside content
(`pos.guard.external`), with suspicious-content signals shown in the log.
The same `(source, ref)` is only processed once.

## Outbound providers

Each one is off until you give it credentials (in `.env`, restart `api`).
While off, an approved action becomes a task for you with the prepared
content, so nothing is sent silently and nothing is lost.

| Action | Settings |
|---|---|
| `email.send` | `POS_SMTP_HOST`, `POS_SMTP_PORT` (587), `POS_SMTP_USER`, `POS_SMTP_PASSWORD` (Gmail: an app password), `POS_SMTP_FROM` |
| `github.comment` | `POS_GITHUB_TOKEN` (fine-grained token, issues: write) |
| `discord.post` | `POS_DISCORD_WEBHOOK_URL` |
| Machine events (knowlage new mail) | `POS_EVENTS_TOKENS=knowlage:<token>`; knowlage sets `KB_EVENTS_URL=http://personalos/api/events` (the web joins knowlage's network kb_default) and `KB_EVENTS_TOKEN=<token>`. The token opens only `POST /api/events`; labels (workspaces, `channel:<domain>`) and optional `headers` feed routing rules and the mail prefilter. |
| GitHub webhook | `POS_GITHUB_WEBHOOK_SECRET`; in GitHub point the webhook at `https://<your host>/api/hooks/github` (events: issues, pull requests, issue comments) |

## Agents and their MCP servers

A worker gets extra MCP servers through `WORKER_CODEX_CONFIG` (entries
separated by `||`), with tokens in its own environment:

```bash
# Dev agent: GitHub MCP
DEV_AGENT_CODEX_CONFIG=mcp_servers.github.url="https://api.githubcopilot.com/mcp/"||mcp_servers.github.bearer_token_env_var="GITHUB_TOKEN"
DEV_GITHUB_TOKEN=<fine-grained token for the ObseumEU repos>
```

Things only the owner can provide: the SMTP / Gmail credentials, GitHub
tokens and the webhook secret, the Discord webhook or bot token, and the
per-agent KB keys on the knowlage server.
