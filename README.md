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
