# Deploy to personalos.obseum.cz (LAN and VPN only)

Target: svr03 (`192.168.1.108`), next to knowlage and Nexus, behind the same
front proxy (`/opt/server/docker-migration/caddy/Caddyfile`) and with the same
access rule as knowlage: `192.168.0.0/16` and the VPN `10.66.66.0/24` (not
`10.66.66.1`), 403 for everyone else. After the move the server is the only
PersonalOS: the API, the web, the agent workers and the deployer all run there.

| What | On the PC | On the server |
|---|---|---|
| Code | git checkout | `/opt/server/personalos/app` (git clone, branch main) |
| Database, files, agent instructions and memory | volume `personalos_pos-data` | same volume name (`COMPOSE_PROJECT_NAME=personalos`) |
| Agents' work folders | `data\agent-work\*` | volumes `personalos_<agent>-work` |
| Dev agent's clone (branch `agent/dev`) | `data\agent-work\dev-agent\PersonalOS` | `/opt/server/personalos/dev-work/PersonalOS` |
| Keys and secrets | `.env` | `/opt/server/personalos/app/.env` (mode 600), from `make_prod_env.py` |
| Codex login | `~\.codex` | `/opt/server/personalos/codex-home` |
| Claude login | `claude` logged in on the PC | `CLAUDE_CODE_OAUTH_TOKEN`, copied from knowlage's `.env` |

## Steps

On the PC (PowerShell, in the checkout):

```powershell
deploy\prod\export-data.ps1 -IncludeCodexLogin -KeepStopped   # stops agents, deployer and API; data\migration-<stamp>\
backend\.venv\Scripts\python deploy\prod\make_prod_env.py data\migration-<stamp>
scp -i $HOME\.ssh\svr03_ed25519 -r data\migration-<stamp> drosko@192.168.1.108:/opt/server/personalos/
```

On the server:

```bash
cd /opt/server/personalos
git clone https://github.com/ObseumEU/PersonalOS app && cd app
install -m 600 ../migration-<stamp>/env.prod .env
# one Claude login for all apps: copy the token from knowlage's .env (never print it)
tok="$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' /opt/server/knowlage/.env | cut -d= -f2-)"
sed -i "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=${tok}|" .env; unset tok
mkdir -p ../codex-home && tar xzf ../migration-<stamp>/codex-home.tgz -C ../codex-home
deploy/prod/import-data.sh ../migration-<stamp>          # checks SHA256SUMS and row counts
git clone . ../dev-work/PersonalOS && git -C ../dev-work/PersonalOS checkout -B agent/dev
git remote add dev /dev-work/PersonalOS                  # the path inside the deployer
docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml \
  --profile agents --profile deploy up -d --build
```

Then add `front-proxy.Caddyfile` to the front proxy next to the knowlage block,
validate it (`caddy validate`) and reload. Nothing else in that file changes.

## Check

- From the LAN or VPN: `https://personalos.obseum.cz/` asks for the password
  (in `Documents\PersonalOS-server-login.txt` on the owner's PC), then Tasks,
  Agents and System show the same data as on the PC.
- `curl -s https://personalos.obseum.cz/api/health` from the LAN; `/mcp` answers
  with 401 without a key.
- From outside (VPN off, phone hotspot): 403.
- System → Runtimes: Codex and Claude (the self-check runs at start).

## Owner steps

- DNS: `personalos.obseum.cz` → the same address as `knowlage.obseum.cz` in the
  router (like knowlage). Until then the PC has a hosts entry.
- GitHub: the server checkout needs read access to `ObseumEU/PersonalOS`, and
  the deployer needs push access to `main` (a deploy key with write access).

## Updates

The deployer does them: whatever the Dev agent commits on `agent/dev` is merged
into main after the checks, pushed and deployed. Manual update:
`git pull && docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml up -d --build api web`.
