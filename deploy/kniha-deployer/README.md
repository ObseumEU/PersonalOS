# Kniha deployer (svr03)

Container `kniha-deployer` (compose here; installed copy in `/opt/server/kniha-deployer`, keys and
state live only there). Every 60 s `sync.sh`:

1. fetches origin in the Kniha team's shared workspace `/work/kniha` (volume `personalos_agent-pool-work`,
   the PersonalOS agent pool) and its `web` submodule;
2. pushes `main` + `agent/*` of ObseumEU/Kniha and `production` + `agent/*` of roskodav/web-builder-studio
   (never force);
3. **web**: when `origin/production` of the web moved, copies it to `/opt/server/rodinne-pribehy`,
   `docker compose up -d --build`, waits for healthy + https://rodinne-pribehy.obseum.cz, rolls the files
   back when the build fails;
4. **app**: when Kniha `origin/main` moved and `app/` or `produkt/` changed, builds the image from a
   `git archive` of that sha (`/opt/server/kniha-test/next`), tags the running image `kniha-test:rollback`,
   starts the new one and waits up to 150 s for `healthy` + https://kniha-test.obseum.cz/healthz.
   Failure: the previous image and source run again (`src/` is only replaced after a healthy start)
   and the sha is marked failed until a new commit lands on main;
5. writes `/work/kniha/.deploy/status.txt` with the **current** state only: what is live, the last
   deploy's result, and the problems of this pass. A good pass clears an old error (T-873 was a false
   alarm from the old status, which showed the last 15 log lines even days later).

For the agents (read-only, no shell on svr03 needed): `/work/kniha/.deploy/status.txt`,
`deploys.log` (every web/app deploy with its result), `last-deploy.log` (web build),
`app-last-deploy.log` (app build, health check, rollback with the container's last log lines).

The app on svr03: `/opt/server/kniha-test/` = `src/` (deployed sha), `.env` (PRISTUP_HESLO, ADMIN_HESLO;
mode 600, owner drosko, never in git), `compose.override.yml` (joins `my-app-network`, so Caddy reaches
`kniha-test:8787`), `e2e/journey.mjs` (Playwright walk of the whole journey). Data: volume
`kniha-test_kniha-test-data`. Caddy: block `kniha-test.obseum.cz` in
`/opt/server/docker-migration/caddy/Caddyfile` (microphone allowed for that origin).

Keys: `keys/id_kniha`, `keys/id_web` = write deploy keys "Kniha deployer" on the two repos.
Log: `state/sync.log`. Stop: `docker compose down` here. Update: write `sync.sh` to a temp file next to
`/opt/server/kniha-deployer/sync.sh` and `mv` it over (sh reads a running script lazily: overwriting it in
place breaks the pass in progress); `compose.yaml` changes need `docker compose up -d`.
