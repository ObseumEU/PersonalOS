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

## Promote mode: merge conflicts

In promote mode (the server: `--promote-from dev/agent/dev`) a branch that does
not merge into main is never retried as the same attempt:

1. the deployer rebases the branch onto the current main **once**, in a
   scratch worktree (the agent's branch is untouched); a clean rebase goes on
   through the tests, the build and the health check like any merge. No
   automatic rebase for commits that touch protected paths (it would drop the
   owner's signatures);
2. otherwise the branch's owner (the author agent, else the Software
   Engineer) gets **one** task per branch, titled "Rebase <branch> onto main",
   with the conflicting files, the hunks and the instruction to rebase; a
   later refusal of the same branch is a comment on it;
3. the tip is **parked**: when main moves, later ticks only check it silently
   (`git merge-tree`, no report, no task) and try it again only once it merges
   cleanly, or when a new commit arrives on the branch.

Every refusal records a one-line reason (`deploys.reason`). The pipeline's
health (attempts, the reject rate, refusals by stage, top reasons, repeats of
the same commit at the same stage) is `GET /api/deploys/health?days=7` and the
`deploy_health` MCP tool (`ops:observe`, the SRE and the Monitor).

## Tests

The tests run in parallel (pytest-xdist): `cd backend && python -m pytest -q -n auto`
(the worker package is on the tests' path, no PYTHONPATH needed). The same
command is the deployer's `DEFAULT_TEST` and CI's (`.github/workflows/tests.yml`,
with `ruff check .` and the web typecheck and build). Tests read policy numbers
(budgets, caps, step caps, spike thresholds) from the code's constants, never
literals, so changing a policy does not break a deploy.
