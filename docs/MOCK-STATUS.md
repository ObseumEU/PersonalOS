# Mock status of the web UI

A small red dot (`MockDot` in `web/src/components/ui.tsx`) marks every part of the
web UI that is mock: sample data, a control that does nothing yet, or a planned
screen. Parts that read real data from the backend API carry no dot.

When you wire a part to real data, remove its dot (drop `mock` on the `Panel`,
the `<MockDot />` / `<SampleBadge />`, or `mock: true` in `web/src/sections.ts`)
and move its row below to "Real".

Rule used for the audit: a widget is real when it renders data from a backend
`/api/...` call; it is mock when it renders `web/src/sample.ts`, hardcoded text,
or a control whose handler does nothing.

## Mock (red dot)

| Screen | Widget | Why it is mock |
| --- | --- | --- |
| Navigation | Calendar, Files, Topics, Notes | Planned screens (`ComingSoon`), no backend yet |
| Navigation | Assistant | Whole page is an example session |
| Navigation rail | SUBSYSTEMS box | Latencies from `sample.ts` (`SUBSYSTEMS`) |
| Today | FIG. 1 Knowledge graph | Random graph with `GRAPH_LABELS` from `sample.ts` |
| Today | Ask box | Submit only calls `preventDefault`; no assistant behind it |
| Today | NOTE Observation | Hardcoded Acme text |
| Today | FIG. 2 Agenda | `EVENTS` from `sample.ts`; no calendar sync |
| Assistant | SESSION Example | `ANSWER` from `sample.ts` |
| Assistant | FIG. 3 Sources in the knowledge graph | Random graph, sample labels |
| Assistant | TAB. 2 Retrieval trace | `ANSWER.trace` from `sample.ts` |
| Assistant | Ask box | Same dead form as on Today |
| System | FIG. 4 Knowledge graph, full | Random graph, sample labels |
| System | Subsystem cards (Knowledge agent, Nexus, Codex CLI) | `SUBSYSTEMS` from `sample.ts`, sparkline is generated |
| Calendar, Files, Topics, Notes | Whole page | `ComingSoon` placeholder |

## Real (no dot)

| Screen | Widget | Backend |
| --- | --- | --- |
| Today | TAB. 1 Today tasks | `tasksApi.list`, `tasksApi.counts`, complete |
| Tasks, Inbox clarify, task detail | Everything | `tasksApi.*` |
| Agents, agent detail | Everything | `agentsApi.*` |
| Network | 3D agent network, ranking | `agentsApi.network` |
| Board | Board columns | `agentsApi.board` (the gates list is static policy text, not data) |
| Approvals | Everything | `agentsApi.approvals`, `decide` |
| Connectors | Setup status, routing rules, events, test event | `/api/connectors`, `/api/routes`, `/api/events` |
| Automations | Jobs, schedules, A2A members and links | `/api/jobs`, schedules API, `/api/a2a/links` |
| System | Runtimes and subscriptions, Deploys | `agentsApi.engines`, `/api/deploys` |
| Shell | Kill switch, approvals badge | `agentsApi.freeze*`, `agentsApi.approvals` |
