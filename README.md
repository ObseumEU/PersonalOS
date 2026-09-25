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
