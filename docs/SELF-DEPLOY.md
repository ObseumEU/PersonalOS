# Self-deploy (step 6)

Agents change their instructions, other agents and the platform itself, and
merge to main without PRs, like we do (AGENTS-SPEC 6). The **deployer**
(`pos.selfdeploy`, service `deployer`, profile `deploy`) keeps that safe:

For every new range of commits on main it

1. checks the constitution: commits that touch protected paths
   (`docs/CONSTITUTION.md`, `pos/guard`, permissions, limits, budget) must be
   signed by the owner (`pos.guard.gitcheck`, `ops/owner_allowed_signers`);
   until the owner sets up signing it only warns; commits that touch
   `shared/tools/` must also pass the tools guard review (`pos.tools`,
   docs/TOOLS.md), which refuses the range on any finding;
2. runs the tests (`DEPLOY_TEST_CMD`);
3. builds and starts (`DEPLOY_UP_CMD`);
4. checks health (`DEPLOY_HEALTH_URL` must answer `{"status": "ok"}`).

If a step fails it **reverts the whole range with one new commit** whose tree
is the last good one (no force-push, no rewritten history), pushes it,
deploys the last good version again, and reports to PersonalOS. The author
(an `Agent: <name>` trailer, else the commit author) gets a task with the
log; if even the redeploy fails, the owner gets a P1 task too. Every deploy is
listed on the System page.

Agent instructions live in git (`agents/<name>/INSTRUCTIONS.md`, mounted
read-only into the API), so changing an agent is a normal commit that goes
through the same checks.

## Running it on the server

```bash
# key: open the "Deployer" member on the Agents page → New key, put it in .env
DEPLOYER_KEY=pos_...
docker compose --profile deploy up -d --build
docker compose logs -f deployer
```

The deployer needs the server's checkout mounted at `/repo` with push access
to `origin`, and the Docker socket to rebuild the stack.
