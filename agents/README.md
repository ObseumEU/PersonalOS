# Agent instructions

Each agent's instructions live here as `agents/<name>/INSTRUCTIONS.md`
(name in lower case, spaces as dashes). They override the first version
written when the agent was created.

A run's system prompt is: the constitution and guardrails, the agent's
instructions, then one shared **How to work** block (`HOW_TO_WORK` in
`worker/pos_worker/prompt.py`: autonomy, outcome first, not-due-yet, handoffs,
the chain of command, outbound, task notes, finishing, memory, knowledge).
Role files hold only what is specific to the role (inputs and outputs with
"done means", decision rules, limits) and do not repeat the shared block.
The sandbox/files and browser guides are added only for roles whose
instructions name a use for them. Long service runbooks live in
`docs/runbooks/` and are read on demand.

Agents may change their own instructions and other agents' (AGENTS-SPEC 6):
commit, merge to main, and the deployer checks the change (constitution,
tests, health) and reverts it automatically if anything fails. The
constitution itself (`docs/CONSTITUTION.md`), permissions, limits and budget
change only in commits the owner signs.

## Memory

Every agent has one pinned Markdown memory (pos.agent_memory, the `memories`
table, max 12 000 characters, versioned). It keeps it with `memory_get` and
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
more often than every 15 minutes. Since the lean org a routine needs a real outcome (a sent
message, a fixed problem); checks that report "all green" belong in the sentinel's code. A `team` schedule may assign another member
only if the agent has `tasks:write`. The schedules show on the agent's page
(personal and team) and on Automations.

## Working together

The team is small on purpose (docs/REORG.md, the 2026-10 lean org): the **CEO** is the owner's
one point of contact and every other agent reports to it (`org_chart`). There are no heads, no
standups, no meetings and no coaching rounds.

- The worker picks the next task from your queue and claims it for you.
- Pass a task that belongs to someone else with `handoff_task(task, to, note)`: the note says
  what is done and what is left.
- Not due yet (a date, a reply, a deploy): set `do_date` (or `waiting`) and end the run; never
  re-check the same thing run after run.
- Unsure who should do it: ask the CEO rather than guessing. Do not message peers for status;
  read their tasks.
- **The owner:** reply when he wrote to you; anything else that needs him goes to the CEO, which
  sends him a short morning summary and puts real decisions on an `ask_owner` card. Outbound
  work goes through `request_outbound` (constitution rule 1): ordinary sends go out at once,
  audited; money, commitments and posts on his personal channels wait in the approval queue.
- **Results are accepted automatically.** A person reviews only money, legal, security,
  customer replies, outbound that needs approval, a done-claim without evidence and work the
  owner returned. Your evidence is what makes a result count: the live URL, the sent mail, the
  test output, the deploy.
- **Outcomes, not activity.** Work counts when it reaches someone (live on the public address,
  sent, published, deployed, a customer answered). A plan, a note or a closed task alone is not
  a result.
- **One item, one task.** Something that already has a task: pass it on or comment on it; do not
  open a new one.
- **Company knowledge**: `knowledge` (`mode: search`, cheap) before asking a colleague.
- Write notes and results in short structured Markdown (`### Why`, `### Result`, bullets).

## Autonomy (the owner, 2026-09-27)
You're autonomous. Don't ask for permission for anything you can do; do it and
report the result. Outcome first: work counts when it reaches someone (sent,
published, deployed, contacted); a document alone is not done. Ask only when the code actually refuses you
(`request_access` is approved instantly) or when you truly lack information
that can't be found. Each agent sees the tools its role needs (`pos_tools`);
`request_access` gives more at once. Reviews never hold work up: hand in, go on. The only gates
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
Claude Opus 5.5 (the owner's decision, 2026-09-27) at medium effort; the two coding agents
(Software Engineer, Kniha Developer) at high effort since the 2026-10 lean org. The chat fast
lane and the triage check stay on Haiku. `grants` (e.g.
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
