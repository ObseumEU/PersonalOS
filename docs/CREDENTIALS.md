# Credentials (Přístupy) — 1Password for agents

Agents use passwords and tokens **without ever seeing them**. The owner keeps
the values in 1Password; PersonalOS keeps only names and references.
Code: `backend/src/pos/credentials/`, `worker/pos_worker/credentials.py`,
web page **Přístupy** (`/credentials`) and a panel on each agent's page.

## Design

| Piece | What it does |
|---|---|
| Registry (`credentials`) | name (`github-deploy`), 1Password reference `op://vault/item/field`, description, env var, HTTP header template, allowed hosts, allowed tools (`command`, `http`), allowed command prefixes (`git push`), max uses per hour, owner notes. **No values.** |
| Grant `cred:<name>` | in `access_grants` (pos.access), optionally scoped `cred:<name>@http`, `@command` or `@<host>`, optionally with expiry. `cred` is an owner-only prefix: the Access manager is refused in code. |
| Resolution | only at execution time, with the 1Password **service account** through the Python SDK (`onepassword-sdk`) in the api process. Values sit in an in-memory cache for at most 5 minutes (`POS_OP_CACHE_SECONDS`), never on disk, in the DB, logs or prompts. |
| Injection | HTTP: the API makes the request itself (`credential_http`), so the value never leaves the API. Commands: the worker's credential runner (`run_with_credentials`) gets the value for one subprocess, puts it in that process's env var and nowhere else. |
| Redaction | every tool result: the plain value, base64 (also inside longer blobs such as HTTP basic auth), URL-encoded and hex forms become `[REDACTED:<name>]`, also when a value is split across output chunks. |
| Audit | `credential_uses` (agent, credential, run, task, tool, host, time, OK/refused with reason) plus `audit_log` (`cred_use`, `cred_refused`). More than `max_uses_hour` uses by one agent in an hour pause that grant (`end_kind = paused`) and DM the owner; one click resumes it. |
| Fail safe | no `OP_SERVICE_ACCOUNT_TOKEN` or `POS_OP_VAULT`, SDK missing, 1Password down: every use is refused with a clear error. Nothing falls back to plain text. |

## How an agent uses a credential

1. `credentials_list` (pos MCP) shows names, what they are for and which it holds.
2. It has none: `request_access(what="capability", capability="cred:github-deploy",
   why="...", task_id="T-12", blocking=true)`. The Access manager cannot grant
   it, so this becomes an **ask_owner ticket** for the owner with the reason
   (and a #team ping). The owner clicks **Schválit** on the Přístupy page (the
   ticket links there): the grant is created, the ticket closes, the agent
   gets the answer in its inbox and its task resumes.
3. It uses it by name:
   - command: `run_with_credentials(command="git push https://x-access-token:{{cred:github-deploy}}@github.com/ObseumEU/PersonalOS.git main")`
     The command guard checks the command as written, PersonalOS checks the
     grant, the allowed command prefixes and hosts (a token is never sent to
     another server) and the hourly limit, logs the use, and returns the
     value to the runner only; the placeholder becomes `${GITHUB_TOKEN}`,
     the output comes back redacted. One plain command: no `; | & $( > <`.
   - HTTP: `credential_http(method="GET", url="https://api.github.com/user", credentials=["github-deploy"])`
     (the configured header) or `{{cred:github-deploy}}` in a header, body
     or URL. HTTPS to the credential's allowed hosts only, no redirects; the
     response comes back redacted and marked as untrusted.

A dedicated tool can also use credentials inside the API. `ha_ssh(command)`
(pos.homeassistant) runs one shell command on the Home Assistant host over SSH
with `ha-ssh` (the password, 1Password item "SSH HomeAssistant") and the
optional `ha-ssh-user` (the user name; without it `POS_HA_SSH_USER`, default
`root`, the Terminal & SSH add-on's user). The credentials allow only the pseudo
command `ha_ssh` and the hosts 192.168.1.56 and homeassistant.local, so they
work nowhere else: not over HTTP and not in `run_with_credentials`. The tool
itself needs the owner's grant `tool:ha_ssh`. The host key is pinned on first
use in `<data>/ha_ssh_known_hosts`.

The worker mounts the runner only for agents holding a credential grant; its
run-bound session token (`/api/worker/credentials/session`, valid while the
run is live) goes to that one MCP server, not to the model.

## Owner setup in 1Password

1. **Plan**: service accounts are available on every current 1Password plan
   (1Password, Families, Teams, Business); a Teams/Business account admin (or
   the individual account's owner) creates them. Rate limits per token/hour:
   Business 10 000 reads, Teams/Families/individual 1 000 reads; per account
   and day: Business 50 000, Teams 5 000, Families/individual 1 000. The
   5-minute cache keeps PersonalOS far below that.
2. Create a vault **PersonalOS Agents** with only what agents may use (not
   Personal/Private/Employee, which service accounts cannot read anyway).
3. Developer → Directory → **Service accounts** → create "PersonalOS svr03",
   access to **that vault only**, permission **Read items** (no write, no
   share). The token is shown once: save it in 1Password.
4. On svr03 put it in the PersonalOS `.env` (read by the `api` container only):
   ```
   OP_SERVICE_ACCOUNT_TOKEN=ops_...
   POS_OP_VAULT=PersonalOS Agents
   ```
   then rebuild/restart `api` (the image installs `onepassword-sdk`).
   Service account access is immutable: for another vault, create a new one.
5. In PersonalOS → **Přístupy** → **Přidat z 1Password**: pick an item's
   field (names only are listed), set the env var/header, allowed hosts and
   commands, save, **Otestovat načtení** (answers OK or the error, never the
   value), then grant it to an agent or approve its request.

## Later: the stacks' own secrets from 1Password (not done yet)

The plaintext secrets in the stacks' `.env` files stay as they are for now.
To move them later, keep a template with references instead of values, e.g.
`.env.tpl`:

```
POS_SESSION_SECRET=op://PersonalOS Server/pos/session-secret
CLAUDE_CODE_OAUTH_TOKEN=op://PersonalOS Server/claude/oauth-token
```

and let the deploy resolve it with the `op` CLI and a *separate* service
account (a server vault, not the agents' vault):

- `op inject -i .env.tpl -o .env` right before `docker compose up` (the file
  exists on disk only on the server, `chmod 600`), or
- `op run --env-file .env.tpl -- docker compose up -d` so the values exist
  only in the environment of that command.

## Proposal for the owner: guard rules (not applied)

`backend/src/pos/guard/` is protected, so this is a proposal only. The
command guard could additionally refuse shell commands that
(a) call `/api/worker/credentials/` or read `POS_CRED_SESSION`/`POS_AGENT_KEY`
(`env`, `printenv`, `/proc/*/environ`), and (b) the external-content scanner
could flag `op://` references in outside content. Today the Bash allow-lists
already keep agents from running such commands; this would make it a
constitution rule rather than a per-worker setting.
