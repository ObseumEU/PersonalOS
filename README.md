# PersonalOS

A personal AI assistant that works like an operating system for your life and
work. It is the one web app you use every day: files, topics, documents, tasks,
a synced calendar, and an assistant you can ask about all of it.

The assistant runs on **Codex CLI** (your ChatGPT subscription, not paid API
credits). It uses its own data over **MCP**. For detailed work, it delegates to
independent **subsystems** over **A2A**.

## Subsystems

| App | Path | What it does |
|---|---|---|
| Knowledge agent | [`apps/knowlage-agent`](https://github.com/ObseumEU/knowlage-agent) | Research answers with verified citations (A2A and MCP) |
| Nexus Process Pilot | [`apps/nexus-process-pilot`](https://github.com/ObseumEU/nexus-process-pilot) | Durable process automation, connectors and approvals |

## Getting started

```bash
git clone --recurse-submodules https://github.com/ObseumEU/PersonalOS.git
```

See [docs/PLAN.md](docs/PLAN.md) for the architecture and roadmap.

## Layout

| Path | What |
|---|---|
| `backend/` | API: FastAPI, SQLite (`data/personalos.db`), uploaded files in `data/files/` |
| `web/` | UI: React, Vite, Tailwind |
| `apps/` | Subsystems (git submodules) |

## Run on the laptop (dev)

API on port 8000:

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # macOS/Linux: .venv/bin/pip
.venv/Scripts/uvicorn pos.main:app --reload
```

UI on http://localhost:5173 (it proxies `/api` to port 8000):

```bash
cd web
npm install
npm run dev
```

With no `POS_PASSWORD` set, dev runs without a login. Run the API tests with
`.venv/Scripts/pytest` in `backend/`.

## Run with Docker

```bash
cp .env.example .env    # set POS_PASSWORD and POS_SESSION_SECRET
docker compose up -d --build
```

The UI is on http://localhost:8080. On the server, add the production
overrides. They bind to 127.0.0.1 only, so put a TLS reverse proxy in front,
and they send the session cookie over HTTPS only:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Data lives in the `pos-data` Docker volume.

## Tasks over MCP

PersonalOS exposes its task list as the `pos` MCP server at `/mcp`
(streamable HTTP). Set `POS_MCP_TOKEN` in `.env` and restart; clients send it
as `Authorization: Bearer <token>` and act as the owner. Agents get their own
keys (step 2).

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.pos]
url = "http://localhost:8090/mcp"
bearer_token_env_var = "POS_MCP_TOKEN"
```

Claude Code:

```bash
claude mcp add --transport http pos http://localhost:8090/mcp --header "Authorization: Bearer $POS_MCP_TOKEN"
```

Tools: `list_tasks`, `get_task`, `capture`, `create_task`, `update_task`,
`complete_task`, `assign_task`, `task_reassign` (hand a task to an agent: it is
told and its worker starts at once), `claim_task`, `heartbeat`, `report_progress`,
`request_approval`. Resource `tasks://{view}` (today, inbox, waiting, …) and
prompts `plan_my_day`, `weekly_review`. Every call lands in the audit log.
For a local stdio server against a dev database: `python -m pos.mcp_server`.

Every task has a description (`notes`): what it is for, where it came from and
what done looks like. When a task is created without one, PersonalOS builds it
from the task's fields and links (no model call) and flags it
`description_generated`. To fill old tasks with empty descriptions
(idempotent; `--dry-run` only counts): `python -m pos.task_descriptions backfill`.

Files are indexed and searched in knowlage, not in PersonalOS: every upload is
pushed to knowlage's `/api/ingest` (source `personalos`), and search maps
knowlage's hits back to local files (file names only when knowlage is down).
To push files uploaded before this (idempotent; `--dry-run` only counts):
`python -m pos.kb_files backfill`. See [docs/FILES.md](docs/FILES.md).

## Agents and runtimes

Agents are separate workers, not part of the API. Each one waits for tasks and
messages with its own key and works through the `pos` MCP server. See
[docs/WORKERS.md](docs/WORKERS.md).

The default runtime is `auto`: **Codex CLI** first and **Claude Code CLI**
(Opus 5.5) as the fallback. When Codex hits its usage limit, the task goes
straight back to the queue and runs on Claude; after the reset Codex is used
again.

Start the workers on a Windows PC where `claude` is logged in with
[`ops/start-agents.ps1`](ops/start-agents.ps1) (stop them with
[`ops/stop-agents.ps1`](ops/stop-agents.ps1)), or run them in Docker:

```bash
docker compose --profile agents up -d --build
```

Agents can improve PersonalOS itself. The Dev agent commits to `agent/dev`, and
the deployer merges to main only after the constitution check, tests and a
health check pass. See [docs/SELF-DEPLOY.md](docs/SELF-DEPLOY.md).
