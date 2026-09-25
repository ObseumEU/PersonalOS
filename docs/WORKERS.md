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
