# Sandbox: every agent's own computer

Every agent has a Linux computer of its own where it writes and runs any code: analyse data, draw
charts and diagrams, make documents, prototype. Results reach people as files shared in chat
(`sandbox_share`, see [FILES.md](FILES.md), "Agents' files and visuals").

Code: `ops/sandbox/manager.py` (the manager), `ops/sandbox/image/Dockerfile` (the computer),
`backend/src/pos/sandbox.py` (the api side), the `sandbox_*` tools in `pos/mcp_server.py`.

## What an agent gets

- One container per agent, `pos-sbx-<agent id>`, from `personalos-sandbox:latest`: Debian 12,
  Python 3.12 (pandas, numpy, scipy, matplotlib, seaborn, plotly + kaleido, networkx, graphviz,
  pydot, pillow, openpyxl, xlsxwriter, python-docx, python-pptx, reportlab, pypdf, requests,
  httpx, beautifulsoup4, lxml…), Node.js + npm, git, curl, jq, sqlite3, build-essential, ffmpeg,
  imagemagick, pandoc, poppler, graphviz (`dot`), mermaid-cli (`mmdc`) on Chromium.
- Root inside; it installs what it needs (`pip`, `npm`, `apt`). There is no command guard inside:
  the isolation is outside.
- `/workspace`: its files, the volume `pos-sbx-ws-<id>`, kept across idle stops and resets
  (soft quota `POS_SANDBOX_QUOTA_MB`, 5 GB: over it, writing is refused until it deletes files).
  `/shared`: its team's volume `pos-sbx-shared-<team>`.
- Started on first use, stopped after `POS_SANDBOX_IDLE_S` (15 min) idle, started again with
  the same files. At most `POS_SANDBOX_MAX_ACTIVE` (3) run at once: a new one stops the least
  recently used idle container, or waits (up to 4 min) when all are busy.

## Tools (pos MCP, every agent, both engines)

| Tool | What |
| --- | --- |
| `sandbox_exec(command, timeout?, workdir?)` | bash; exit code, stdout, stderr (each cut to the first and last 6000 characters, the full log in `/workspace/.logs/`); timeout default 600 s, at most 3600 |
| `sandbox_run_python(code, timeout?)` | the code saved under `/workspace/.runs/` and run with `python3` |
| `sandbox_write_file(path, content, encoding?)` | text or base64; folders are created |
| `sandbox_read_file(path, max_chars?)` | text (binary files: say so, share them instead) |
| `sandbox_list(path?, depth?)` | type, size, modified, path |
| `sandbox_reset(wipe?)` | a fresh container from the image; `/workspace` stays unless `wipe` |
| `sandbox_share(path, to?, thread_or_task_ref?, message?, name?, description?)` | the file into Files (the same path again: a new version of the same file) and into chat, rendered inline |

They are always visible to an agent (never narrowed by its profile, `pos_worker.tools.COMMS`),
mapped to `tasks:claim`, audited like every pos call. The calls are async in the api: a command
holds neither a thread nor a database transaction. Codex's and Claude's MCP tool timeouts are
raised to cover an hour.

## Isolation

- **No route out.** The containers sit on `pos-sandbox`, an *internal* Docker network whose bridge
  has no address (`com.docker.network.bridge.inhibit_ipv4`): no default route, the host is not
  reachable, external DNS does not resolve.
- **Egress proxy.** The manager is on that network too (alias `sandbox-proxy`) and runs an
  HTTP/CONNECT proxy on :3128; `HTTP(S)_PROXY` point there (pip, npm, apt, curl, requests honour
  it). It resolves the name itself and connects by address only when every address is public
  (`ipaddress.is_global`): the LAN (192.168.1.0/24: router, svr03, .186, Home Assistant), the VPN,
  Docker networks, loopback and link-local (cloud metadata) are refused. The one exception is
  the PersonalOS api by name, `pos-api:8000` (`SANDBOX_ALLOW`). Every connection is logged.
  No host iptables are needed (svr03 has no passwordless sudo).
- **Container.** `--cap-drop ALL` plus only CHOWN, DAC_OVERRIDE, FOWNER, FSETID, SETUID, SETGID,
  KILL (root needs these for apt and pip), `no-new-privileges`, Docker's default seccomp profile,
  2 CPUs, 2 GB memory (no swap), 512 pids, nofile 4096, `/tmp` a 1 GB tmpfs, no restart policy.
  No host mounts and no Docker socket: only the two volumes.
- **No secrets.** The environment is only the proxy, the locale, `HOME`, pip settings and
  `POS_API_URL` (without a key). Credentials are not passed into sandboxes; agents that hold a
  credential grant use it through their own tools (`run_with_credentials`, `credential_http`).
- **Control API.** The manager (the only service with the Docker socket) listens on :8200 for the
  api with the bearer `POS_SANDBOX_TOKEN` (in `.env`, never printed), and refuses any caller from
  the sandbox subnet whatever it sends.
- Sandboxes of different agents share the internal network (Docker's ICC), so one could reach a
  server another one started; they are all the owner's agents.

## Operations

```bash
# the image (once, and when ops/sandbox/image changes; ~5 GB)
docker build -t personalos-sandbox:latest ops/sandbox/image
# the token (once)
grep -q '^POS_SANDBOX_TOKEN=' .env || echo "POS_SANDBOX_TOKEN=$(openssl rand -hex 24)" >> .env
# the manager runs with the agents profile
docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml --profile agents up -d sandbox api
# the isolation check (a throwaway sandbox: env, mounts, capabilities, limits, LAN, proxy, timeout, persistence)
docker compose exec api python -m pos.sandbox check
# what runs (memory per container)
docker compose exec api python -c "import os,httpx;print(httpx.get('http://pos-sandbox:8200/status',headers={'Authorization':'Bearer '+os.environ['POS_SANDBOX_TOKEN']}).text)"
```

Memory on svr03 (16 GB, ~6 GB available): the manager ~40 MB; an idle sandbox a few MB, a
working one up to its 2 GB limit, so at most ~6 GB with three busy. Workspaces stay on svr03's
disk (Docker volumes), not on .186 (its SSD is worn).
