# PersonalOS: plan

Status: draft for discussion. Written 2026-09-25.

## 1. What PersonalOS is

PersonalOS is a personal AI assistant that works like an operating system for
one person's life and work. It has two parts:

- **A small kernel.** This is the assistant you talk to. It understands the
  request, remembers context about you, and delegates the work to the right app.
- **Apps.** Each app is an independent agent in its own repository, added here
  as a git submodule under `apps/`. An app can be installed, updated or removed
  without touching the kernel.

The kernel should stay thin. Real capabilities live in the apps, and the kernel
only routes, remembers and schedules.

## 2. Design principles

1. **Codex CLI first, to save credits.** Every LLM call goes through
   `codex exec`, which runs on the ChatGPT subscription. That means there is no
   per-token billing. Paid APIs are an explicit opt-in fallback only, never the
   default. Both first apps already work this way (section 4).
2. **Standard protocols, not custom glue.**
   - **MCP** is for tools and data. It lets an agent call a function or read a
     resource.
   - **A2A** is for agent-to-agent delegation. One agent hands a whole task to
     another, then streams progress and gets an artifact back.
3. **Apps are autonomous.** An app can run without PersonalOS, and PersonalOS
   knows it only through its A2A agent card and its MCP endpoint.
4. **Self-hosted and private.** Everything runs in Docker Compose on your own
   machine or home server, reachable over LAN or VPN.

## 3. Architecture

```text
 You (CLI now; Telegram or web later)
        │
        ▼
 ┌──────────────────────────── PersonalOS kernel ────────────────────────────┐
 │  codex exec  ◄── the brain (ChatGPT subscription)                          │
 │     │  MCP tools configured for Codex:                                     │
 │     ├─ pos-memory    personal memory: facts, preferences, people, goals    │
 │     ├─ pos-a2a       list_agents / ask_agent / get_task (A2A client bridge)│
 │     └─ app MCP servers (direct tool access where an app exposes one)       │
 │  registry: apps.yaml (name, agent-card URL, MCP URL, how to start)         │
 │  scheduler: routines such as a morning brief or weekly review              │
 │  exposes its own A2A agent card and MCP server, so other agents can use it │
 └───────────────┬──────────────────────────────────┬─────────────────────────┘
                 │ A2A (JSON-RPC, streaming)        │ A2A / MCP
                 ▼                                  ▼
        apps/knowlage-agent                  apps/nexus-process-pilot
        research answers with                durable process automation,
        verified citations                   connectors, approvals
```

**The key idea is that Codex CLI already is the agent loop.** Codex CLI can
load MCP servers from its config. So the kernel does not need its own
LLM-orchestration code. It runs `codex exec` with a curated set of MCP servers,
and one of those, `pos-a2a`, turns "delegate this to app X" into an A2A call.
That keeps the kernel small and puts every token on the subscription.

### Request flow (example)

1. You ask: "What should I do about churn in the gym? Then make it a task."
2. The kernel runs `codex exec` with the `pos-memory` and `pos-a2a` MCP servers.
3. Codex calls `ask_agent("knowledge", …)`. The bridge sends an A2A
   `SendStreamingMessage` to knowlage-agent and returns the answer artifact
   with its citations.
4. Codex calls `ask_agent("nexus", …)` to create or run a process. Nexus keeps
   the run durable and asks for approval where its policies require it.
5. The kernel stores what it learned in `pos-memory` and replies to you.

## 4. The first two apps (as found on 2026-09-25)

### knowlage-agent (`apps/knowlage-agent`)

- **What it does:** a business advisor over video and interview transcripts. It
  answers in Czech with verified citations (video, timestamp and the exact
  passage). It has workspaces as separate knowledge bases and a web UI on
  `:8080`.
- **Stack:** Python 3.12, FastAPI, Qdrant, Voyage embeddings and rerank, and a
  React frontend from Lovable.
- **Brain:** Codex CLI through the ChatGPT subscription (`src/kb/codex.py`),
  with a Codex critic pass that checks the citations.
- **Protocols:** it already has both.
  - **A2A 1.0 server** (`src/kb/a2a_server.py`): an agent card at
    `/.well-known/agent-card.json`, where each workspace is a tenant, plus
    JSON-RPC `/a2a` with streaming, and REST.
  - **MCP server** (`src/kb/mcp_server.py`): the `search`, `read_section`,
    `read_document`, `list_documents` and `fetch_page` tools.
- **Costs:** Codex is on the subscription. Voyage has 200M free tokens.
- **Integration effort:** low. Register its agent card and it works.

### nexus-process-pilot (`apps/nexus-process-pilot`)

- **What it does:** a self-hosted platform for process automation and agents.
  It covers durable runs, typed tools, approval checkpoints, knowledge
  retrieval, browser automation, connectors (Google, M365, GitHub, Discord,
  Notion, SSH, MCP), schedules and an audit trail.
- **Stack:** a TypeScript pnpm/Turborepo monorepo, with a Fastify API, a
  TanStack web app, Postgres and Redis.
- **Brain:** `@nexus/llm` with a Codex CLI provider (`packages/llm/src/codex-cli.ts`,
  subscription-based `codex exec`). To make it Codex-first, set
  `LLM_PRIMARY_PROVIDER=codex-cli` (see `.env.example`). The default order
  otherwise starts with Claude Code CLI.
- **Protocols:**
  - **MCP:** it acts as an MCP client for connections (the `mcp` auth type in
    `packages/connectors`). It also has an optional read-only knowledge MCP
    server at `/mcp/knowledge` (`KNOWLEDGE_MCP_ENABLED=true`).
  - **A2A:** none found. This is the main gap.
- **Integration effort:** medium. It needs an A2A facade (an agent card, plus
  a `SendMessage` that starts a run and a task mapped to run status) or an MCP
  server exposing "start run / get run / approve".

## 5. Roadmap

| Phase | Deliverable | Notes |
|---|---|---|
| 0 | Repo skeleton, submodules and this plan | This PR |
| 1 | Kernel MVP: the `pos` CLI wraps `codex exec`, with `apps.yaml`, the `pos-a2a` bridge MCP server, and knowlage-agent wired up | First end-to-end answer |
| 2 | `pos-memory` MCP server (SQLite plus Markdown), so the assistant remembers you | Reviewable, editable memory |
| 3 | Nexus A2A facade (a PR in nexus-process-pilot), registered in PersonalOS | Nexus becomes a delegate |
| 4 | PersonalOS exposes its own A2A card and MCP server | Usable from Claude, Codex or other agents |
| 5 | Routines (morning brief, weekly review) and a chat interface (Telegram or web) | Proactive assistant |
| 6 | More apps: calendar, email, tasks, finance | Each one a submodule with A2A and MCP |

## 6. App contract (what every app must provide)

- An **A2A agent card** at `/.well-known/agent-card.json` that lists its skills.
- **A2A JSON-RPC** with `SendMessage`, `SendStreamingMessage`, `GetTask` and
  `CancelTask`.
- Optionally, an **MCP server** for fine-grained tools.
- **Codex CLI** as the default model runtime, using the shared `~/.codex` login.
- A `docker compose` service definition and a health endpoint.
- Auth with a bearer API key, for the kernel only.

## 7. Open questions

1. **Kernel language:** Python or TypeScript? Recommended: Python. The official
   `a2a-sdk` and `mcp` packages are mature there, and knowlage-agent already
   uses both.
2. **First interface:** CLI, Telegram or web? Recommended: CLI first, then
   Telegram.
3. **Where it runs:** a laptop, or an always-on home server? Routines need an
   always-on host.
4. **Submodule tracking:** should the submodules be pinned to commits (the
   current setup, which is reproducible), or follow `main` automatically?

## 8. Working with submodules

```bash
git clone --recurse-submodules https://github.com/ObseumEU/PersonalOS.git
# or, in an existing clone:
git submodule update --init --recursive
# pull the latest app versions:
git submodule update --remote --merge
```
