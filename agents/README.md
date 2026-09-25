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
