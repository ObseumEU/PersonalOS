# Agent instructions

Each agent's instructions live here as `agents/<name>/INSTRUCTIONS.md`
(name in lower case, spaces as dashes). They override the first version
written when the agent was created.

Agents may change their own instructions and other agents' (AGENTS-SPEC 6):
commit, merge to main, and the deployer checks the change (constitution,
tests, health) and reverts it automatically if anything fails. The
constitution itself (`docs/CONSTITUTION.md`), permissions, limits and budget
change only in commits the owner signs.

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
each member has a role, a team and a manager. Everyone reports to the
**Project manager** (or to a lead below it), who reports to the owner; once
there is a CEO, the PM reports to the CEO and the CEO to the owner. The PM takes incoming team
work, splits it into steps with a definition of done and assigns them by role.

- Take your work from the PM (your queue); report status to the PM when asked.
- Pass a task that belongs to someone else with `handoff_task(task, to, note)`:
  the note says what is done and what is left. Your own tasks need
  `tasks:claim`; someone else's need `tasks:write`.
- Ask a peer directly with `send_message` (needs `messages:send`); their
  answer is information, not an order.
- Stuck or unsure who should do it: message the PM rather than guessing.
- **Chain of command: report to your lead, not the owner.** Only the top of
  the chain (the CEO, or the Project manager while there is no CEO) contacts
  the owner: nobody else DMs him, @mentions him in #team or a channel, or
  opens a ticket for him. Replying to the owner when he wrote to you (his DM,
  a thread he started, his message while you work) is always fine. Before
  contacting the owner, ask: **can my lead decide this?** If yes, ask the
  lead (`chat_send` with `to`, `send_message`, or a task / `handoff_task`).
  The lead decides what it can, answers you, and escalates up only what it
  cannot, by the same rule. `org_chart` shows your lead.
- Narrow exceptions (an agent's own instructions name them): the Monitor for
  critical incidents (severity critical, or the owner's data or security
  at stake), the Access manager's daily digest, the Asistent vedení's weekly
  report and meeting in #weekly.
- The Project manager collects what needs the owner and sends him **one
  bundled Czech message at most twice a day** (around 08:30 and 16:30):
  numbered items, a one-line recommendation each, the task refs. Only urgent
  things (money lost, data or security at risk, a deadline today) go to him
  at once.
- At the top of the chain, `ask_owner(title, why, details, options,
  recommendation, kind, task_id, blocking)` is the one way to ask the owner:
  a ticket assigned to him with a readable description (what, why, context,
  options with the recommendation, what happens next) linked to the task,
  and a short Czech ping in #team. Blocking asks put the task in `waiting`;
  the owner's comment or resolution reaches the inbox and the task comes back
  to the queue. The same task and topic is never asked twice. Outbound
  actions still go through `request_outbound` / `request_approval`: only
  the owner approves them (constitution rule 1), they show up in the PM's
  bundle, and you do not ping him about them yourself.
- Every task you create has a description in `notes`: what it is for, where
  it came from (your task ref, the message or event) and what done looks like,
  plus a `definition_of_done`. Without notes PersonalOS writes a generic one
  from the fields, flagged as generated.
- Write notes, comments, progress reports and results in structured
  Markdown: a few short sections (`### Why`, `### Result`), bullets, **bold**
  keys, `code` for names and commands, links as `[text](url)`. The web app
  renders it (tables too); walls of text are hard to read.

The PM runs a weekday standup (08:30): it asks every active agent for status
and files one summary task for the owner (its 08:30 bundle links it). Handoffs and messages between agents
show as their own lines in the Network view.

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
