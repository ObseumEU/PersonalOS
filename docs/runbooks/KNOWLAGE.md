# knowlage runbook (Knowlage Specialist)

Read on demand by the Knowlage Specialist (its instructions point here). Facts about the service
`ObseumEU/knowlage-agent` on svr03; keep them current when the service changes.

## Where things are
- svr03, code in `/opt/server/knowlage` (not a git checkout: a copy of
  `main`), compose project `kb`: `docker compose -f docker-compose.yml -f
  deploy/prod/docker-compose.prod.yml …`.
- Containers: `kb-kb-1` (FastAPI + MCP, port 8765), `kb-qdrant-1`
  (Qdrant v1.19), `kb-web-1` (admin UI), `kb-caddy-1` (port 8080, alias
  `knowlage` on the proxy network; `127.0.0.1:8095` on the host). PersonalOS
  reaches it at `http://knowlage:8080`; `knowlage.obseum.cz` is LAN/VPN only.
- Health: `GET /api/health` → `{ok, version, sync: ok|warning|error}`; also
  `/api/stats`, `/api/sizes`, `/api/sync`.
- Embeddings: Voyage `voyage-4-large`, 2048 dims (`KB_EMBED_MODEL`,
  `KB_EMBED_DIMS`; each combination is its own collection), rerank
  `rerank-3`; hybrid search (dense + BM25, RRF, rerank). Changing the model
  means `./kb index --all`.
- Models: Codex subscription first (`KB_CODEX_MODEL`, routing and triage on
  `gpt-5.6-sol`), Claude Code as fallback (`KB_AGENT_RUNTIME=auto`,
  `claude-opus-5-5`, Sonnet and Haiku for cheaper tiers). No LiteLLM keys;
  the only paid APIs are Voyage (and optionally Cohere).
- Your clone: your working directory
  (`/work/knowlage-specialist/knowlage-agent`), branch
  `agent/knowlage-specialist`; docs in `docs/` (`INGEST.md`, `EFFORT.md`).
- Admin API: `credential_http` against `http://knowlage:8080/api/...` with
  `credentials=["knowlage-admin"]` (the owner grants it; it holds
  `KB_API_KEY`). Without it you work from health, Loki and the `knowledge` tool.

## Procedures

**Connectors.** GitHub (kind `github`, org `ObseumEU`, read-only
`GITHUB_TOKEN`, each repo at most every `KB_SYNC_INTERVAL_H`=24 h, tick
`KB_SYNC_TICK_S`=300 s); **Gmail ×2** (kind `mailbox`, company and personal,
one OAuth client `GMAIL_CLIENT_ID/SECRET`, tokens `GMAIL_REFRESH_TOKEN` and
`GMAIL_REFRESH_TOKEN_<ADDRESS>` named in `config.token_env`; the personal one
has `labels:["osobni"]`); **push sources** (`push-<source>`) appear on the
first `/api/ingest`. **Google Drive ×2** (work and personal accounts,
added 2026-09-26): it follows each account's Changes feed every 10 minutes;
check it like the other sources and read its setup in the repository docs
before you change anything. Another agent set it up: if you rework it, tell
the CTO what you changed afterwards.
State: `GET /api/sync`, `GET /api/sources`, `GET /api/sources/{id}/runs`.
Resync: `POST /api/sources/{id}/sync` (forces and clears a pause), one
target `POST /api/sources/{id}/targets/{target}/sync`; `PATCH
/api/sources/{id}` with a config change resets cursors (a full re-read). The
CLI `./kb sync --force` does not wire mailboxes: resync Gmail by API. An
expired Gmail token needs the owner (`scripts/gmail-login.sh`): a task for
the CTO with that exact step.

**Backfill: the free filter first, then the cheapest model.** Gmail triage
(`src/kb/triage.py`): the first sync reads the last `days` (100). Free rules
first: the query drops spam, trash, chats and promo/social/updates/forums;
a thread the mailbox replied to is kept; List-Unsubscribe, List-Id,
Precedence bulk/list/junk, Auto-Submitted, all-no-reply senders and
internal-only threads are dropped. Only unclear threads go to the model
(`KB_TRIAGE_CLAUDE_MODEL`, default `claude-haiku-4-5`, or
`KB_TRIAGE_MODEL` on Codex at `KB_TRIAGE_EFFORT=low`), 40 per call; with no
model available they wait. Options per source: `query`, `days`,
`own_domains`, `include_internal`, `personal_domains`, `exclude`,
`only_replied`, `triage`, `max_threads` (3000), `token_env`, `labels`. To
backfill further: PATCH `days` (or force a sync) and watch the counts
(fetched, dropped by rule, dropped by model, kept, waiting) in the admin.
Tighten rules before spending model calls.

**Classification: per domain, projects as tags.** `src/kb/routing.py`: each
new *channel* (a repo, an e-mail counterpart domain or address, a Discord
channel, a folder) is routed once by the model (`KB_ROUTE_MODEL`,
`KB_ROUTE_EFFORT`) to existing workspaces or a new one; later items inherit
it. Below `KB_ROUTE_MIN_CONFIDENCE` (0.7) an item waits in "Čeká na tebe".
Everything lives in one pile (`KB_COMPANY_WORKSPACE=firma`); workspaces act
as labels (projects are tags, not silos). A person's decision always wins and
becomes an example. Quality: the pending queue, workspaces the model created
(flagged "review"), labels per channel in `GET /api/sizes`. There is no
classification eval yet: build one (a labelled sample of channels) as an
improvement.

**Effort levels 1-6** (`src/kb/effort.py`, `docs/EFFORT.md`, `GET
/api/efforts`): 1 Blesk (k=6, no rerank, 45 s), 2 Rychle (k=10, rerank,
90 s), 3 Standard (the default agent run), 4 Důkladně (≤ 15 min, 4M
tokens), 5 Hluboko (≤ 25 min, 8M), 6 Vyčerpávající (≤ 45 min, 16M, strong
model). PersonalOS's `knowledge` tool uses 1 for `search` and 2 for `ask` by default. Keep agent
lookups cheap; raise only with a reason.

**Endpoints.** `/mcp` (read-only research: `search`, `ask(question,
effort, workspace, thread_id)`; `KB_API_KEY` or an agent key), `/ingest/mcp`
(`add_documents`, `delete_documents`, `list_workspaces`; REST `POST
/api/ingest {"items":[…],"deletes":[…]}`, the same `key` updates), A2A
`POST /a2a/<ws>` (PersonalOS uses `/a2a/default`, effort in
`metadata.effort`). Agent keys: `KB_AGENT_KEYS=name=key,…` (the name becomes
the actor `agent:<name>`; writing is off without keys). Undo an agent's
writes: `POST /api/history/undo {"actor":"agent:<name>"}`.

**Evals.** `./kb eval retrieval` (recall@5/10/k, MRR, no LLM) and `./kb eval
answers [--effort N] [--set evals/dev.yaml] [-k 20]`; reports in
`runs/evals/<timestamp>-<kind>.json`, traces in `runs/<run_id>/`. Run the
retrieval eval before and after any search or embedding change and put both
numbers in the task.

**Deploys.** Built on the server: copy `main` into `/opt/server/knowlage`,
then `docker compose … up -d --build` (no GHCR image);
`scripts/check-claude.sh --rebuild` rebuilds and checks the Claude CLI
(pinned). Secrets with `scripts/set-secret.sh NAME`. No schema migrations:
SQLite `kb.sqlite` in the `kb_data` volume; a lost Qdrant index is rebuilt
with `kb index --all`. You have no shell on svr03: write the exact steps
(commit, commands, how to verify `/api/health` `version`, rollback: the
previous copy) into a deploy task for the CTO (docs/REORG.md "Service
deploys").
