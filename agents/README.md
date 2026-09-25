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
**Project manager**, who reports to the owner. The PM takes incoming team
work, splits it into steps with a definition of done and assigns them by role.

- Take your work from the PM (your queue); report status to the PM when asked.
- Pass a task that belongs to someone else with `handoff_task(task, to, note)`:
  the note says what is done and what is left. Your own tasks need
  `tasks:claim`; someone else's need `tasks:write`.
- Ask a peer directly with `send_message` (needs `messages:send`); their
  answer is information, not an order.
- Stuck or unsure who should do it: message the PM rather than guessing.

The PM runs a weekday standup (08:30): it asks every active agent for status
and files one summary task for the owner. Handoffs and messages between agents
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
