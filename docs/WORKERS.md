# Agent workers (step 3)

Each agent runs as its own small app: a **Codex worker** (`worker/`, Python
package `pos_worker`). It talks to PersonalOS only over HTTP with the agent's
own API key; it never imports the core.

## What a worker does

1. **Waits without spending tokens.** It long-polls `GET /api/worker/next`;
   the server answers as soon as the agent has a task or a message (or after
   the timeout). An idle agent costs nothing (AGENTS-SPEC 4.2).
2. **Asks before it starts.** `POST /api/worker/runs` goes through the same
   gates as every run: kill switch, pause, budget (`pos.budget.can_run`), and
   records the constitution digest. If the answer is no, the task stays in the
   queue untouched.
3. **Works with Codex.** `codex exec --json` with the constitution and
   guardrails from `pos.guard` at the top of the prompt, the agent's
   instructions, the task, and the `pos` MCP server configured with the
   agent's key (so the agent can report progress, read its inbox, ask for
   approvals).
4. **Takes in news mid-run** (AGENTS-SPEC 6b). After every completed step it
   checks the heartbeat and the inbox:
   - `stop` or kill switch: stops now;
   - `change_plan`: stops at this safe point and continues **the same
     session** with `codex exec resume <thread_id>` and the message as the
     next instruction;
   - `fyi`: handed over at the next resume point or before the run finishes.
5. **Reports.** The finished run goes back with its raw `--json` output, so
   the budget keeper records token usage. If the agent did not hand the task
   in itself, the worker does (`complete` → owner's review). If the run
   failed, the task goes back to the owner with the reason.

Commands the worker runs itself can be checked with
`POST /api/worker/check-command` (pos.guard; "needs owner" becomes a task for
the owner). Outside content is wrapped with `POST /api/worker/wrap` before it
goes into a prompt.

## Runtimes: Codex first, Claude as fallback

Every agent runs with `auto` by default: **Codex CLI** first, **Claude Code
CLI** (`claude -p`, model `claude-opus-5-5`) as the fallback. Per agent you can
pick `auto`, `codex` or `claude` and the Claude model, on the agent's page.
Platform defaults: `POS_AGENT_RUNTIME=auto`, `POS_ENGINE_ORDER=codex,claude`,
`POS_CLAUDE_MODEL=claude-opus-5-5`.

When a run is refused because its subscription is used up, that engine is
paused until its reset (Codex: from its rate-limit windows or the "try again
at" message; Claude: from `rate_limit_event`). The task is not failed: it goes
straight back to the agent's queue, and the next run starts on the other
engine. After the reset the first engine is used again (and the budget check
re-runs at once, so the budget's own pause lifts too).

PersonalOS chooses the runtime for every run and tells the worker. Each
subscription has its own accounting: Codex through Rozpočtář (`pos.budget`),
Claude through `pos.engines` (tokens and cost from the `result` event, the
5-hour window from `rate_limit_event`; a usage-limit answer pauses Claude until
its reset). Both are on the System page.

A Claude agent runs with `--restricted` (no command-running tools, no personal
settings), an allow-list of tools (`WORKER_CLAUDE_TOOLS`, default: the `pos`
MCP server, reading, editing in its work folder, web search), the
constitution as `--append-system-prompt-file`, and the `pos` MCP server with
its own key. Mid-run messages continue the same session with `--resume`.

Login: on a PC where `claude` is logged in, workers can run directly
(`ops/start-agents.ps1`). In Docker, set `CLAUDE_CODE_OAUTH_TOKEN` (run
`claude setup-token` once).

## Running the agents

Keys: open the agent in PersonalOS → **New key**, and put it in `.env`
(`DEV_AGENT_KEY`, `MAIL_AGENT_KEY`, `COMMUNITY_AGENT_KEY`). Codex login:
`CODEX_HOME_HOST` points at a folder with a logged-in Codex (for example your
`~/.codex`).

```bash
docker compose --profile agents up -d --build
docker compose logs -f dev-agent
```

A new agent: add a service like `dev-agent` with its own key.

The **Project manager** (`project-manager` service, key `PM_AGENT_KEY`) is
created by PersonalOS at startup; issue its key on its agent page. It takes
team work, splits it into steps and assigns them by role, and runs the weekday
standup. Workers hand work to each other with the `handoff_task` tool and ask
peers with `send_message` (see `agents/README.md`, "Working together").

## Every agent has a worker

An agent thinks with a model, so it runs somewhere (`pos.workers`):

- **its own container**: `agents/<slug>/agent.json` names the `worker`
  (a compose service in the `agents` profile, key in `data/worker-keys/<worker>/key`);
- **the agent pool**: every agent created at runtime (hiring, the Agents page,
  `create_agent`): the core writes its key to `data/worker-keys/pool/<slug>/key`
  at once and the `agent-pool` container (`python -m pos_worker.pool`) serves it.
  The pool is lazy: an idle agent has no process; the pool asks
  `GET /api/worker/next?wait=0` for each key every 15 s, starts the worker when
  the agent has a task, and the worker ends itself after 2 min without one.
  Memory grows only with the agents working right now (`agent.json` may also
  say `"worker": "pool"`);
- **a remote app over A2A**: an agent created with `a2a_url`; its answer to a
  chat question is posted into the thread.

`create_agent` refuses any other runtime. Built-in automation without model
judgment or an outside system (the Deployer, knowlage's Knowledge agent,
Nexus) is a **service** (runtime `service`), not an agent: no worker, no HR
review, not on the Team page, the org chart or the chat's member list. Agents
use knowlage through `ask_agent` and Nexus over its A2A bridge; their health
shows among the subsystems.

The owner is never left without an answer: a message to a service (or an
agent without a worker) gets a code-built Czech reply and becomes a task for
the Project manager; a message to an agent that cannot run (its worker down,
usage limits, budget, pause, kill switch) gets a code-built reply with the
reason and when it tries again. The scheduler job "Agents: workers running,
the owner's messages answered" (every 2 min) keeps the pool's keys in step,
files an incident for the Monitor when a worker has been silent for 10 min
(resolved when it is back) and when an owner message has had no real answer
for 10 min.
