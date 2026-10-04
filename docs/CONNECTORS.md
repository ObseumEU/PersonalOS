# Connectors (step 4)

Three kinds of traffic, each with one owner:

| Direction | Who does it | How |
|---|---|---|
| **Incoming events** → tasks | PersonalOS core (`pos.routing`) | Connector agents call MCP `emit_event`; GitHub calls `/api/hooks/github`; you can post a test event on the Connectors screen. |
| **Knowledge** (e-mails, threads, repos into the knowledge base) | knowlage-agent | knowlage ingests e-mail with its own Gmail connector and syncs GitHub itself. Agents may still push other material with their own KB key (`/ingest/mcp`), but the Mail agent does not. PersonalOS does not copy data. See `apps/knowlage-agent/docs/INGEST.md`. |
| **New mail → the Mail agent** | PersonalOS core (`pos.routing`, `pos.mailfilter`) | One `gmail` event per new message or thread (`POST /api/events`, or `emit_event`). A rule-based prefilter (no model) drops mailing lists (List-Unsubscribe, List-Id), Precedence bulk/list, no-reply/notification senders, Gmail's Promotions/Social/Updates/Forums and auto-submitted mail; they are stored and counted (Connectors), never run. Rules: `POS_MAIL_PREFILTER` (a JSON file, or `off`). The rest becomes a task for the Mail agent, which only reacts; replies go out directly (audited, reviewed daily by the CEO). |
| **Outbound actions** (e-mail, GitHub comment, Discord post) | PersonalOS core (`pos.outbound`) | Agents call MCP `request_outbound`; ordinary sends run at once (audited, daily review digest to the CEO); money, commitments and posts on your personal channels run only after you approve them in Approvals (constitution Ú1). |

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

Every send goes through the ledger (`pos.outbound_ledger`): idempotent per (action, thread or target,
content hash), rate-limited (per agent and day, 3 e-mails a day to one address, a company cap), audited
(`outbound:<action>`), in the CEO's 18:30 digest and in `outbound_stats(days)` (`GET /api/outbound/stats`,
MCP `outbound_stats`). `POS_OUTBOUND_DRY_RUN=1` builds and audits everything and sends nothing.
A connector that is off returns `not_configured` (an approved item becomes a task for you).

| Action | How it goes out | Settings |
|---|---|---|
| `email.send` | **A Gmail draft** in the right mailbox and thread with your signature; you send it from one "Čeká na tebe" item (grouped per `campaign`); the sync (`outbound_drafts`, every 10 min) sees you send or discard it and closes the item; "důvěra v koncepty" counts unchanged / edited / discarded. Nastavení → "E-maily odesílat automaticky" switches to `auto` (`outbound.email.mode`; per-domain exceptions `outbound.email.auto_domains`). | drafts: `POS_GMAIL_COMPOSE_TOKEN_<ADDRESS>` (`gmail.compose`); auto: `POS_GMAIL_SEND_TOKEN_<ADDRESS>` (`gmail.send`, one consent: `ssh -t -L 8767:127.0.0.1:8767 svr03 /opt/server/personalos/app/deploy/prod/gmail-send-login.sh <address>`; obtained for david.rosko@obseum.cz on 2026-10-04) |
| `github.comment`, `github.issue`, `github.review`, `github.pr` | at once | `POS_GITHUB_TOKEN` |
| `discord.post` | at once | `POS_DISCORD_WEBHOOK_URL` (a channel webhook: Discord → channel → Integrations → Webhooks) |
| `linkedin.post` | **always your approval first**; the card shows the text and the image; then the Posts API, or "připraveno k publikaci" (your item with the text, and a "Publikovat na LinkedIn" button once connected) | see below |
| `payment`, `web.post` | never automatic: approved, a task for you | – |
| Kniha web | not an action: the `production` branch and the kniha-deployer | – |

### LinkedIn (your steps, once)

1. https://www.linkedin.com/developers/apps/new → app "PersonalOS" (company page: Obseum), logo, accept the terms.
2. Products: add **Sign In with LinkedIn using OpenID Connect** and **Share on LinkedIn** (both self-serve).
3. Auth → Authorized redirect URLs: `https://personalos.obseum.cz/api/integrations/linkedin/callback`.
4. Put the Client ID and Primary Client Secret in `/opt/server/personalos/app/.env` as
   `POS_LINKEDIN_CLIENT_ID` and `POS_LINKEDIN_CLIENT_SECRET` (or hand them to the deployer); restart `api`.
5. Open https://personalos.obseum.cz/api/integrations/linkedin/start (logged in, on the LAN/VPN) and allow
   `openid profile w_member_social`. The token is stored encrypted in `<data>/secrets/linkedin.bin`; about
   60 days later you get one item to reconnect.

| Other | Settings |
|---|---|
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

Things only the owner can provide: the Gmail consents, the LinkedIn app, GitHub
tokens and the webhook secret, the Discord webhook or bot token, and the
per-agent KB keys on the knowlage server.
