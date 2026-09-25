# PersonalOS: plan

Status: draft for discussion. Updated 2026-09-25 with the owner's decisions.
How agents work (lifecycle, budget, constitution, kill switch): [AGENTS-SPEC.md](AGENTS-SPEC.md).

## 1. What PersonalOS is

PersonalOS is **the one place I open every day**, to manage my stuff and ask
questions. It is the main system. The subsystems (apps) do the deep, detailed
work behind it.

- **PersonalOS (daily use).** A web app for files, topics, documents, tasks
  and a synced calendar. It also has an assistant I can ask about any of it.
  It has an admin section, but daily use comes first.
- **Subsystems (detailed admin work).** These are independent systems with
  their own UIs for power-user and admin tasks. PersonalOS uses them in the
  background over A2A and MCP, and links to them from the admin section.

| | PersonalOS | Subsystems |
|---|---|---|
| Who and when | Me, every day | Me, when I need detail or admin |
| Examples | See today's tasks and calendar, find a document, upload a file, ask a question | Build an automation in Nexus; manage knowledge workspaces and indexing |
| Talks to the other | Calls subsystems over A2A and MCP | Stays autonomous, unaware of PersonalOS |

## 2. Decisions

| Topic | Decision |
|---|---|
| Brain | **Claude Code CLI** (`claude -p`, model claude-opus-5-5, Claude subscription), **Codex CLI** (`codex exec`, ChatGPT subscription) as the fallback. No paid API by default. |
| Protocols | **MCP** for tools and data, **A2A** for delegating to subsystems |
| Backend | **Python** (FastAPI) |
| Primary interface | **Web** (React; Lovable-compatible like the other apps) |
| Where it runs | Developed on the laptop; the target is the server (Docker Compose) |
| Subsystems | Git submodules under `apps/`, **pinned to commits** |

## 3. Daily-use features (native in PersonalOS)

These live in PersonalOS itself, because they are what I use every day:

1. **Home / Today.** Today's calendar, open tasks due soon, recent files, and a
   question box.
2. **Files and documents.** Upload (drag and drop), browse, preview, tag, and
   full-text search. Files are stored on disk and the metadata in the database.
3. **Topics.** One place per life or work area (a client, a project, "health",
   "house"). It holds the related files, notes, tasks, events and
   conversations.
4. **Tasks.** Open and closed, with due dates, priority and topic. There is a
   quick-add, and the assistant can create them.
5. **Calendar (synced).** Pulled from Google or Microsoft 365 and shown next to
   tasks. It starts read-only; creating events can come later.
6. **Notes.** Markdown documents that belong to topics.
7. **Assistant.** A chat about everything above. For example: "What's open for
   client X this week?", "Summarize the contract I uploaded yesterday", or
   "Plan my Friday". Answers link back to the files, tasks or events they used.
8. **Admin.** Subsystem status and health, links into their UIs, and settings
   such as the Codex login and connected accounts.

## 4. Architecture

```text
 Browser (laptop / phone)
        │
        ▼
 ┌──────────────────────── PersonalOS (docker compose) ─────────────────────┐
 │  web (React)  ──►  api (FastAPI, Python)                                 │
 │                      │                                                    │
 │                      ├─ db: SQLite (files meta, topics, tasks, notes,    │
 │                      │       calendar cache, chat history) + FTS5 search │
 │                      ├─ storage: uploaded files on disk (volume)         │
 │                      ├─ sync: calendar sync job (Google / M365)          │
 │                      └─ assistant: runs `codex exec` with MCP servers:   │
 │                           • pos      PersonalOS's own data as MCP tools  │
 │                           │          (search_files, read_file,          │
 │                           │           list_tasks, create_task,          │
 │                           │           list_events, topics, notes)       │
 │                           └─ pos-a2a  list_agents / ask_agent / get_task │
 │  also exposes: /mcp (its MCP server) and an A2A agent card               │
 └───────────────┬───────────────────────────────┬──────────────────────────┘
                 │ A2A                            │ A2A (after a facade)
                 ▼                                ▼
        knowlage-agent                     nexus-process-pilot
        deep research over documents       automations, connectors,
        with verified citations            approvals (nexus.obseum.cloud)
```

The main points:

- **Codex CLI is the agent loop.** PersonalOS does not implement LLM
  orchestration. It starts `codex exec` with MCP servers, so every token stays
  on the subscription. It uses the same pattern knowlage-agent uses today: the
  Codex login is kept in a Docker volume and set up once from the admin page.
- **PersonalOS's own data is an MCP server (`pos`).** The assistant uses it.
  The same endpoint lets Codex or Claude on the laptop work with my tasks and
  files too.
- **Subsystems are only reached over A2A.** The `pos-a2a` bridge MCP server
  gives Codex the ability to delegate a task and stream the result.
- **SQLite fits here**, because this is a single-user system. It needs no extra
  service, backup is one file, and FTS5 handles search. Postgres can come later
  if needed.

### Which subsystem does what

| Need | Handled by |
|---|---|
| Quick search in my own files | PersonalOS (FTS5) |
| Deep question over many documents or transcripts, with citations | knowlage-agent (A2A). Files uploaded to PersonalOS can also be pushed into a "personal" knowledge workspace. |
| Automations, scheduled processes, approvals, external connectors | Nexus (A2A, after a facade) |
| Calendar sync | PersonalOS first, directly with read-only Google/M365 access. If Nexus connections already hold those accounts, reuse them later. |

## 5. The two subsystems (as found on 2026-09-25)

### knowlage-agent (`apps/knowlage-agent`)
- It gives Czech business advice over transcripts and documents, with verified
  citations. It has workspaces as separate knowledge bases and a web UI.
- Stack: Python, FastAPI, Qdrant and Voyage.
- Brain: Codex CLI (`src/kb/codex.py`).
- It already has an **A2A 1.0 server** (`src/kb/a2a_server.py`) and an **MCP
  server** (`src/kb/mcp_server.py`).
- Status: **not deployed on the server yet.** It needs a deployment next to
  PersonalOS.

### nexus-process-pilot (`apps/nexus-process-pilot`)
- A self-hosted platform for agents and process automation: durable runs,
  approvals, connectors, knowledge and browser automation.
- Stack: TypeScript, Fastify, Postgres and Redis.
- Brain: `@nexus/llm` with a Codex CLI provider. Set
  `LLM_PRIMARY_PROVIDER=codex-cli`.
- It has **MCP** (client connections, plus a knowledge MCP server) but **no
  A2A**. It needs an A2A facade (agent card; `SendMessage` starts a run; task
  status maps to run status).
- Status: **running on the server** at `https://nexus.obseum.cloud/`.

## 6. Roadmap

| Phase | Deliverable | Result |
|---|---|---|
| 0 | Submodules and this plan | This PR |
| 1 | Skeleton: FastAPI, React shell, SQLite, Docker Compose (dev and server), single-user login | The app runs on the laptop and on the server |
| 2 | Tasks, Topics, Files (upload, browse, search), Notes | Daily use starts |
| 3 | Assistant: `codex exec`, the `pos` MCP server, chat with links to sources | Ask questions about my stuff |
| 4 | Calendar sync and the Home / Today page | Complete daily overview |
| 5 | Subsystems: deploy knowlage-agent, add the `pos-a2a` bridge, and an admin section with health checks | Deep research from PersonalOS |
| 6 | Nexus A2A facade (a PR in nexus-process-pilot), then delegate automations | Automations from PersonalOS |
| 7 | Routines (morning brief, weekly review) and notifications | Proactive assistant |

## 7. Subsystem contract

Every subsystem must provide:

- an **A2A agent card** at `/.well-known/agent-card.json`, plus JSON-RPC with
  `SendMessage`, `SendStreamingMessage`, `GetTask` and `CancelTask`;
- optionally, an **MCP server** for fine-grained tools;
- **Codex CLI** as the default model runtime;
- a health endpoint and bearer-key auth for PersonalOS;
- its own UI for detailed admin work, which PersonalOS links to.

## 8. Open questions

1. **Calendar provider:** Google, Microsoft 365, or both?
2. **Login:** a single password or passkey, or "Sign in with Google"?
3. **Network:** will PersonalOS run on the same server as Nexus, and is Nexus
   reachable from it (same host, or over the VPN)?
4. **Phone:** is a responsive web app enough, or is an installable PWA wanted
   later?

## 9. Working with submodules

```bash
git clone --recurse-submodules https://github.com/ObseumEU/PersonalOS.git
# or, in an existing clone:
git submodule update --init --recursive
# move an app to a newer commit (pinned, so this is a deliberate change):
git -C apps/<app> fetch && git -C apps/<app> checkout <commit>
git add apps/<app> && git commit -m "Bump <app>"
```
