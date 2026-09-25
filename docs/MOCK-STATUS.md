# Mock status of the web UI

A small red dot (`MockDot` in `web/src/components/ui.tsx`) marks every part of the
web UI that is mock: sample data, a control that does nothing yet, or a planned
screen. Parts that read real data from the backend API carry no dot.

Dots sit on the navigation item, on each mock panel, and on single mock items
inside otherwise real pages. Hovering a dot shows why it is mock (the `why`
text, or the `mock="..."` string on a `Panel` or in `web/src/sections.ts`).

When you wire a part to real data, remove its dot (drop `mock` on the `Panel`,
the `<MockDot />` / `<SampleBadge />`, or `mock` in `web/src/sections.ts`)
and move its row below to "Real".

Rule used for the audit: a widget is real when it renders data from a backend
`/api/...` call; it is mock when it renders hardcoded sample text,
or a control whose handler does nothing.

## Mock (red dot)

| Screen | Widget | Why it is mock |
| --- | --- | --- |
| Navigation | Calendar, Files, Topics, Notes | Planned screens (`ComingSoon`), no backend yet |
| Today | FIG. 2 Agenda | No calendar sync yet (honest empty state) |
| Calendar, Files, Topics, Notes | Whole page and each planned feature row | `ComingSoon` placeholder |
| Every page header | STATUS "API online" | Static text; not a live health check |
| Tasks (Today view) | "Today's plan" capacity bar | Fixed 6 h focus day (`DAY_CAPACITY_MIN`) until calendar sync; the task minutes are real |
| Connectors | `calendar` in the source lists | No calendar connector emits events yet (no dot: it is an option in a select) |

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
| Shell | SUBSYSTEMS box | `/api/system/subsystems` (live probes of knowlage, Nexus, runtimes) |
| Today, Assistant, System | FIG. 1, 3, 4 Knowledge graph | `/api/knowledge/graph` (our knowlage: workspaces, sources, collections, documents) |
| Today, Assistant | Ask box, answers, cited passages | `/api/knowledge/ask` (knowlage, verified citations) |
| Today | Handed in by agents (was the NOTE) | `tasksApi.list("review")` |
| System | Subsystem cards | `/api/system/subsystems` |
