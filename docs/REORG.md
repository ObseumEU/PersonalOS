# Reorganisation into a company (2026-09)

The owner asked (2026-09-26): *reorganise all the agents, redo the whole
hierarchy so it makes sense; delete them all and recreate the structure so it
is like a company, with everything a company should have, plus specialists
for Home Assistant, Nexus and knowlage who maintain and manage them.*

"Delete" means **archive** (constitution Ú3): every old agent is archived with
its history, and a new member takes over its work. Two members are kept in
place because the platform's code and safety rest on their identity (below).

## The org chart

```
Board: David (Owner)
└── CEO ................................. Opus 5.5, medium
    ├── Chief of Staff (Asistent vedení) . Sonnet 5, medium
    │   └── Executive Assistant ......... Sonnet 5, low     (the old Assistant, renamed in place)
    ├── COO ............................. Sonnet 5, medium   (projects, standup, cross-team work)
    ├── CTO ............................. Opus 5.5, medium
    │   ├── Software Engineer ........... Codex → Opus 5.5  (agent/dev, GitHub issues)
    │   ├── QA Reviewer ................. Sonnet 5, medium   (reviews agent/dev before the deployer)
    │   ├── SRE ......................... Sonnet 5, low      (svr03, 186, sentinel, backups)
    │   │   └── Hlídač (on-call) ........ Haiku 4.5, low     (incident triage)
    │   ├── Nexus Specialist ............ Codex → Opus 5.5, low
    │   ├── Knowlage Specialist ......... Codex → Opus 5.5, low
    │   ├── Home Assistant Specialist ... Sonnet 5, medium   (kept, created 2026-09-26)
    │   └── Security Engineer ........... Sonnet 5, low
    ├── CFO ............................. Sonnet 5, low
    ├── Access manager (Správce přístupů)  Opus 5.5, low     (kept in place)
    ├── Head of People (HR) ............. Haiku 4.5, low
    │   └── Performance Coach ........... Sonnet 5, low
    ├── Head of Growth (+ sales) ........ Sonnet 5, low
    │   ├── Content & Brand ............. Sonnet 5, medium
    │   └── Community Manager ........... Haiku 4.5, low     (dormant until Discord is connected)
    ├── Head of Customer Success (+ support)  Sonnet 5, low
    └── Legal & Compliance .............. Sonnet 5, medium   (dormant: tasks only)

Services (no agent, no worker): Deployer, knowlage ("Knowledge agent"), Nexus.
```

21 role agents plus the Home Assistant Specialist. Every definition is
`agents/<slug>/agent.json` (role, team, lead, permissions, budget class,
engine and model, effort, worker profile, own budget, routines) and
`INSTRUCTIONS.md` (Czech with people; responsibilities, KPIs, what it
decides alone and what goes to its lead, the chain of command, its routines
and tools). The old files are in `agents/_archive/`.

### Changes to the proposal, and why
- **Sales / Business Development merged into the Head of Growth.** One seat
  would only forward leads to another; the head does the pipeline itself
  (heads are doers) and leads Content & Brand and the Community Manager.
- **Customer Support merged into the Head of Customer Success.** The head
  would have one report doing all of its work: a pure forwarding hop per
  mail event. The mail rule routes straight to the head.
- **The Executive Assistant is the old Assistant renamed in place** (actor
  id 2), not a new agent: that actor is the platform's system identity
  (scheduler, suggestions, the `ai` alias) and holds the owner's DM history.
- **The Access manager is kept** (same id): its hard limits and its own
  grants (owner-only) are bound to it; only its lead changes (the CEO).
- **The CEO's owner contact is batched through the Chief of Staff**: the CEO
  is the only agent that contacts the owner; the CoS's digest (08:40 and
  16:30) and weekly report are the CEO office's channel. Named exceptions:
  the Hlídač's critical incidents (≤ 3 a day), the Access manager's digest
  (code), the HA Specialist's safety OKs.
- **The SRE does not duplicate the daily health digest**: the sentinel's
  code-built digest stays with the Hlídač; the SRE's daily routine is a
  2-call capacity check that says nothing when green.
- **The standup no longer messages every agent** (that woke ~20 runs a day):
  the COO reads the board and asks only the stuck.
- **Services reply through the CEO**: a message to the Deployer, knowlage or
  Nexus gets the code reply and becomes a task for the CEO (the COO where
  there is no CEO yet).

## Chain of command
Report to your lead (`org_chart`), not the owner. The lead decides what it
can and passes up only what it cannot. Only the CEO contacts the owner;
replying to the owner when he wrote to you is always fine. Outbound actions
wait in the approval queue (the CoS lists them in the digest). A rule in the
instructions, not a gate in code (docs/TEAM.md).

## Workers and memory
Every agent runs in the **agent pool** (`agent.json` `"worker": "pool"`);
the eleven per-agent containers are gone. The pool is lazy (no process while
an agent has no work; the supervisor is one small process) and runs at most
`POOL_MAX_RUNNING` (4) workers at once, each run's CLI ~200 MB. Idle, the org
costs no tokens and no worker memory; the old containers took ~200 MB idle.

One pool container serves agents with different needs, so each agent's
worker settings come from its file (`"effort"` and `"profile"`, served in
`/api/worker/me`, applied by `pos_worker`): `pos_tools` (narrowed tool list),
`claude_tools` / `claude_builtin` / `claude_disallowed`, `max_usd_run`,
`max_steps`, `workdir` (under `/work` or `/repos` only). The narrow tool
lists of the Hlídač, the Access manager, HR, the CoS and the coach moved from
the compose file into their `agent.json`.

The Software Engineer keeps its clone: the pool mounts
`${POS_DEV_WORK}/PersonalOS` at `/work/PersonalOS` (the same path as before,
so its venv still works); the QA Reviewer reads it with read-only tools; the
deployer still promotes `agent/dev` from it. The Nexus and Knowlage
Specialists get their own clones in the pool volume
(`/work/<slug>/<repo>`).

## Cost
List-price equivalent (Claude runs are on the subscription; Codex runs
first where the engine is `auto`, on the ChatGPT subscription).

| | per day |
|---|---|
| Idle (nothing to do) | $0: long-poll probes only, no model call |
| Routines on a quiet weekday (CoS digest ×2, standup, SRE check, Nexus and knowlage passes, coach, amortised weeklies) | ~$1.2 |
| Typical day (plus ~8 mail tasks, ~5 chat questions, ~3 owner requests to the CEO, ~3 incidents, 2 GitHub issues with QA review) | ~$5 |

Opus only where judgment or code pays for it: the CEO, the CTO, the
Software Engineer and the specialists' code work. Routine roles run on Haiku
(Hlídač, HR, Community) or Sonnet at low effort. Each agent has its own
budget in `agent.json` (usd_day / usd_month / usd_run / runs_day), set once
at creation; after that the Access manager and the owner own it.

## Old agent → new role

| Old agent | New member | How | Moves with it |
|---|---|---|---|
| Project manager | COO | archived, new | open tasks; its standup archived (the COO gets a cheaper one); the code role `PM_NAME` |
| Asistent vedení | Chief of Staff | archived, new | open tasks; the `weekly_report` job (found by role `chief_of_staff`); #weekly (the CEO joins) |
| Assistant | Executive Assistant | **renamed in place** (id 2) | everything (history, DMs, `ai` alias); `agents:create` revoked (hiring is HR's) |
| Dev agent | Software Engineer | archived, new | open tasks and reviews; the GitHub rules; the clone and `agent/dev` |
| Monitor | Hlídač | archived, new | open incidents; the sentinel and Grafana rules; its budget |
| Mail agent | Head of Customer Success | archived, new | open tasks; the mail rule |
| Community agent | Community Manager | archived, new | the Discord rule; its daily digest archived (Discord is not connected) |
| HR agent | Head of People | archived, new | open tasks (incl. unanswered owner DMs); the code role `HR_NAME` |
| Agent coach | Performance Coach | archived, new | open tasks; its review (now weekdays) |
| Access manager | Access manager | **kept** | lead → CEO; runs in the pool with the same narrow tools |
| Home Assistant Specialist | same | kept | lead → CTO; `cred:home-assistant` stays |
| Knowledge agent, Nexus, Deployer | services | unchanged | lead → CTO (not on the chart) |

Routing rules after the move: GitHub issue labelled `agent` → Software
Engineer; new e-mail → Head of Customer Success; invoice e-mail (`faktur|
invoice|rechnung`) → CFO (was the disabled Nexus rule; now on); Discord →
Community Manager; sentinel incident and Grafana alert → Hlídač.

**Grants**: new members get their `agent.json` permissions as grants (plus
`outbound:*` for those with `approvals:request`; every send still waits for
approval). Owner-only grants done in the migration as the owner, per his
request: the CFO `tool:access_usage` and the Security Engineer
`tool:access_audit` (read-only numbers); the Executive Assistant loses
`agents:create`. Credentials stay the owner's: `cred:home-assistant` stays
with the HA Specialist; the owner creates when he wants them `github-nexus`,
`github-knowlage` (push of the specialists' branches), `knowlage-admin`
(`KB_API_KEY`) and `litellm-read` (spend) in 1Password and grants them on
the Přístupy page.

**Keys**: the archived agents' PersonalOS keys are revoked by the archive;
the pool writes keys for the new members. The `*_AGENT_KEY` lines in `.env`
are no longer read by anything (their containers are gone) and their keys
are revoked; they stay until the owner cleans `.env` up. knowlage's `KB_AGENT_KEYS` and the
LiteLLM virtual keys `pos-*` (`/opt/server/litellm/agent-keys.env`) are not
used by PersonalOS agents today (`access/litellm.py` is off); they stay as
they are, to be blocked by the CFO/Security review, not deleted. The
knowlage stack is not touched.

**Budgets**: the old agents' budgets stay on the archived rows (history); the
new members get theirs from `agent.json`. HR's active-agent limit is 26
(default in `pos.hr.policy`, the setting `hr.max_active_agents`): the new
company is ~21 non-system agents plus room to hire.

## How to run it (phase B)
1. Back up: `data/personalos.db` (sqlite `.backup`) and `.env` on svr03.
2. Merge to main and push; pull on svr03.
3. `docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml --profile agents up -d --build --remove-orphans`
   (api, web, agent-pool; the eleven agent containers are removed; their
   volumes stay). The API start-up creates the new members from git.
4. Dry run, then apply, inside the api container:
   `python -m pos.reorg`, then `python -m pos.reorg --apply --announce`.
   It renames the Assistant, sets every lead from the files, moves the
   routing rules, archives the old schedules, moves open tasks and reviews,
   does the owner grants, renames jobs, archives the old agents and posts the
   CEO's announcement in #team. It is idempotent.
5. Set up the specialists' clones in the pool volume
   (`/work/nexus-specialist/nexus-process-pilot`,
   `/work/knowlage-specialist/knowlage-agent`, branches
   `agent/<slug>`).
6. Verify: every agent answers an owner DM (a chat task for its worker); a
   test GitHub-issue event reaches the Software Engineer; a test incident
   reaches the Hlídač; the Team page shows the chart.
7. Optional, after a week: `DEPLOY_REQUIRE_REVIEW=1` for the deployer (the
   QA gate, `pos.deploy_review`).

**Rollback**: restore the DB backup and `.env`, check out the previous main
and bring the old compose up. Nothing was deleted, so a partial rollback is
also possible: `restore` an archived agent on its page.

## Service deploys (knowlage, Nexus)
The specialists commit on their branch and, with the credential, push it and
open a pull request (outbound: approved). There is no agent deploy lane on
svr03 yet: a deploy is a task with the exact commands, reviewed by the SRE
and run by a person. Next step (a Software Engineer task): a second deployer
instance per app (the same `pos.selfdeploy` with the app's repository, test
and up commands and health URL), gated by the QA review.
