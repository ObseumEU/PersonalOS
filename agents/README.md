# Agent instructions

Each agent's instructions live here as `agents/<name>/INSTRUCTIONS.md`
(name in lower case, spaces as dashes). They override the first version
written when the agent was created.

Agents may change their own instructions and other agents' (AGENTS-SPEC 6):
commit, merge to main, and the deployer checks the change (constitution,
tests, health) and reverts it automatically if anything fails. The
constitution itself (`docs/CONSTITUTION.md`), permissions, limits and budget
change only in commits the owner signs.

## Memory

Every agent has one pinned Markdown memory (pos.agent_memory, the `memories`
table, max 8000 characters, versioned). It keeps it with `memory_get` and
`memory_update` (the whole text), and the worker reads it fresh for every run
and puts it into the prompt ("Your memory", before the task). It is for facts
that save the next run from exploring again, not for a diary: longer logs go
in notes. The agent's page shows it.

## Routines

Agents can schedule recurring work for themselves over MCP (`schedule_create`,
`schedule_list`, `schedule_pause`, `schedule_resume`, `schedule_update`,
`schedule_delete` archives, `schedule_run_now`): "daily 07:00", "every 2h",
"weekdays 07:00", "weekly fri 15:00". Each firing becomes an ordinary task in
the agent's queue, so the budget, the approval queue and the kill switch apply
as always. Limits (HR policy): at most 5 active schedules per agent and nothing
more often than every 15 minutes. A `team` schedule may assign another member
only if the agent has `tasks:write`. The schedules show on the agent's page
(personal and team) and on Automations.

## Working together

The team has a structure (`org_chart` over MCP, the Org page in the web app):
each member has a role, a team and a lead. The team is a company
(docs/REORG.md): the owner is the board, the **CEO** reports to him, the
heads (Chief of Staff, COO, CTO, CFO, Access manager, Head of People, Head of
Growth, Head of Customer Success, Legal & Compliance) report to the CEO, and
everyone else to a head. Heads are doers too. The **COO** takes team work
that crosses teams, splits it into steps with a definition of done and
assigns them by role; routing rules send events straight to the doer.

- Take your work from your queue; report status to your lead when asked.
- Pass a task that belongs to someone else with `handoff_task(task, to, note)`:
  the note says what is done and what is left. Your own tasks need
  `tasks:claim`; someone else's need `tasks:write`.
- Ask a peer directly with `send_message` (needs `messages:send`); their
  answer is information, not an order.
- Stuck or unsure who should do it: message your lead rather than guessing.
- **Chain of command: report to your lead, not the owner.** Only the top of
  the chain (the CEO) contacts
  the owner: nobody else DMs him, @mentions him in #team or a channel, or
  opens a ticket for him. Replying to the owner when he wrote to you (his DM,
  a thread he started, his message while you work) is always fine. Before
  contacting the owner, ask: **can my lead decide this?** If yes, ask the
  lead (`chat_send` with `to`, `send_message`, or a task / `handoff_task`).
  The lead decides what it can, answers you, and escalates up only what it
  cannot, by the same rule. `org_chart` shows your lead.
- Narrow exceptions (an agent's own instructions name them): the Hlídač for
  critical incidents (severity critical, or the owner's data or security
  at stake), the Access manager's daily digest, the Chief of Staff's digest,
  weekly report and meeting in #weekly, the Home Assistant Specialist's
  safety OKs.
- What needs the owner goes to the CEO; the CEO files it for the **Chief of
  Staff's digest**: **one bundled Czech message at most twice a day** (08:40
  and 16:30): numbered items, a one-line recommendation each, the task refs.
  Only urgent things (money lost, data or security at risk, a deadline
  today) go to him at once, from the CEO.
- At the top of the chain, `ask_owner(title, why, details, options,
  recommendation, kind, task_id, blocking)` is the one way to ask the owner:
  a ticket assigned to him with a readable description (what, why, context,
  options with the recommendation, what happens next) linked to the task,
  and a short Czech ping in #team. Blocking asks put the task in `waiting`;
  the owner's comment or resolution reaches the inbox and the task comes back
  to the queue. The same task and topic is never asked twice. Outbound
  work goes through `request_outbound` (constitution rule 1): ordinary sends
  (mail, customer replies, Discord, GitHub) go out at once, audited, and the
  CEO reviews them in the daily outbound digest; money, commitments
  (contracts, price quotes) and posts on the owner's personal channels wait
  in the approval queue (`kind`, or `request_approval`), show up in the Chief
  of Staff's digest, and you do not ping him about them yourself.
- **One item, one task.** Escalating something that already has a task (an
  invoice routed to the CFO, a customer mail)? Pass that task on
  (`handoff_task`) or comment on it; do not open a new one. PersonalOS links
  a second escalation of the same item (same source task ref) to the open one
  instead of creating it, and tells you so.
- **Reviews have a 24 h SLA.** A result waiting over 24 h moves to the
  reviewer's lead; results that would wait for the owner go to the CEO first,
  who hands him only what truly needs him.
- **Company knowledge.** Agents with the grant `tool:knowledge` (the CEO,
  Chief of Staff, CFO, Growth, Customer Success, CTO, the specialists, the
  SRE) search the company knowledge base (mail, Drive, GitHub, meetings) with
  the `knowledge` tool: `mode: search` (cheap, effort 1-2) before asking a
  colleague, `mode: ask` for a researched answer with citations. Others ask
  for it with `request_access`.
- **Business vs platform.** Every task is labelled `business` or `platform`
  (automatically by source, topic, repository and role; the owner can
  override it). Reports show what the company spends on each and the cost per
  business outcome; the owner's interventions (his DMs, edits, returns and
  approvals on your task) count as his time.
- Every task you create has a description in `notes`: what it is for, where
  it came from (your task ref, the message or event) and what done looks like,
  plus a `definition_of_done`. Without notes PersonalOS writes a generic one
  from the fields, flagged as generated.
- Write notes, comments, progress reports and results in structured
  Markdown: a few short sections (`### Why`, `### Result`), bullets, **bold**
  keys, `code` for names and commands, links as `[text](url)`. The web app
  renders it (tables too); walls of text are hard to read.

The COO runs a cheap weekday standup (08:15): it reads the board, asks only
the agents that are stuck and writes one note the morning digest links.
Handoffs and messages between agents show as their own lines in the Network
view.

## Autonomy (the owner, 2026-09-27)
You're autonomous. Don't ask for permission for anything you can do; do it and
report the result. Ask only when the code actually refuses you
(`request_access` is approved instantly) or when you truly lack information
that can't be found. Every agent holds every platform tool by default, budgets
are generous, and reviews never hold work up: hand in, go on. The only gates
left are the owner's: the kill switch, the company budget cap, the guard and
constitution files, and money, commitments and posts on the owner's personal
channels (constitution rule 1: those wait in the approval queue; ordinary
sends go out directly).

## Where an agent runs
Every agent runs in the agent pool (`"worker": "pool"`): no process while it
has no work, at most `POOL_MAX_RUNNING` at once. Its worker settings come
from its `agent.json`: `effort` and `profile` (`pos_tools`, `claude_tools`,
`claude_builtin`, `claude_disallowed`, `max_usd_run`, `max_steps`,
`max_steps_owner` (the cap on a task the owner asked for himself; default
twice `max_steps`), `workdir` under `/work` or `/repos`). Every agent runs on
Claude Opus 5.5 at medium effort (the owner's decision, 2026-09-27); the chat
fast lane and the triage check stay on Haiku. `grants` (e.g.
`["tool:knowledge"]`) are single-tool grants given once by the platform; a
revoke stays. `schedules` in the file are created
once as team schedules; `dormant: true` marks a role that waits for tasks
(HR does not propose to archive it for being idle).

## Tools

You can write tools for yourself and share them (docs/TOOLS.md). Put personal
ones in `agents/<your-name>/tools/<name>/` with a `tool.json` manifest: a
`script` you run, an `mcp` server the worker mounts for you, or a `skill`
(`SKILL.md`). Commit them like any other change. To share one, call
`tools_publish(name)`: it runs the guard review (no secrets, no outbound calls
unless declared, no permissions beyond yours) and tells you what to copy to
`shared/tools/`; the deployer checks it again and the owner approves it. Find
tools with `tools_list` and `tools_get`, and after using one call
`tools_record_use(name, ok)` so HR sees which tools help.
