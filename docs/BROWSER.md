# Browser use and computer use

Agents get a real web browser, and for tasks that need a real GUI a desktop,
like Claude has. Both are MCP servers the worker mounts into a run **only with
the grant**, for **both engines** (Claude Code via `--mcp-config`, Codex via
`-c mcp_servers.*`); both go through the same PersonalOS guard.

| | Browser (`tool:browser`, older `browser:use`) | Computer (`tool:computer`) |
|---|---|---|
| What | [Playwright MCP](https://github.com/microsoft/playwright-mcp) (`@playwright/mcp`, pinned in `worker/Dockerfile`), headless Chromium, behind `worker/pos_worker/browser_guard.py` with the tools of `browser_tools.py` | the desktop sandbox `ops/desktop` (Xvfb + fluxbox + Chromium + xdotool + ImageMagick) behind `worker/pos_worker/computer.py` |
| Tools | see below (Claude-style: navigate, get_page_text, read_page, find, click/type by ref or coordinate, batch, ...) | `computer_screenshot`, `computer_zoom`, `computer_left_click` (+ right, middle, double, triple), `computer_left_click_drag`, `computer_mouse_move`, `computer_scroll`, `computer_type`, `computer_key`, `computer_wait`, `computer_open_url`, `computer_cursor_position` |
| Starts | on the run's first browser call (the tool list is static, so the CLI starts at once) | on the first `computer_*` call |
| Ends | with the run (the CLI closes the MCP server) or after `BROWSER_MAX_MINUTES` (30) | with the run, or after `DESKTOP_IDLE_S` (300) idle / `DESKTOP_MAX_S` (1800) |
| Isolation | a fresh in-memory profile per run (`--isolated`); kept logins only with the owner's `browser:profile` (below) | a fresh profile per session in tmpfs, deleted with it |
| Memory | Chromium flags (`--js-flags=--max-old-space-size=256`, 2 renderers); at most `BROWSER_MAX_CONCURRENT` (3) browsers in the pool at once, the others wait for a slot; one above `BROWSER_MAX_MB` (1200, proportional memory: PSS) is restarted | idle ~15 MB, a session ~220 MB; container `mem_limit` 1 GB; **one session at a time**, the next run waits in line (`DESKTOP_QUEUE_WAIT`, 600 s) |

## The browser tools

The guard shows the agent its own tool set (not Playwright MCP's raw one); each
maps onto a Playwright MCP tool or onto a short Playwright snippet the guard runs
itself (`browser_run_code_unsafe`, which is code execution in the pool and is
never offered to the agent). Playwright MCP runs with `--snapshot-mode none`, so
an action answers with one line (`Now: <title> — <url>`) instead of the whole
page tree; the agent reads when it needs to.

| Tool | What |
|---|---|
| `browser_navigate(url)` | open a URL, or `back` / `forward`; a cookie banner is declined (only the necessary cookies) |
| `browser_get_page_text(max_chars, whole_page)` | the readable text (main content; tables as tab-separated rows): the cheapest read |
| `browser_read_page(filter, ref, depth, max_chars)` | the accessibility tree with refs; `filter='interactive'` keeps only controls and headings |
| `browser_find(query)` | elements by a description in plain words ("search box", "Log in button") with their refs |
| `browser_click(ref \| coordinate, clicks, button, modifiers)` | click by ref or at a point; double and triple click |
| `browser_type(text, ref?, submit)` | into a field by ref, or into the focused element |
| `browser_fill_form`, `browser_select`, `browser_hover`, `browser_drag`, `browser_scroll`, `browser_key` | forms, dropdowns, pointer, scrolling, keys (`ctrl+a`, `Enter`, `PageDown`) |
| `browser_wait(text \| text_gone \| network_idle \| seconds)` | for an app that loads its data after the page (at most 30 s) |
| `browser_tabs(action)` | list, new, select, close |
| `browser_screenshot(scale, full_page, ref)` | a JPEG (default half size, ~1,500 tokens), capped per run (`BROWSER_MAX_SCREENSHOTS`, 15) |
| `browser_zoom(region)` | a region at full resolution (small text, icons); counts toward the cap |
| `browser_console(level, pattern)`, `browser_network(filter \| index)` | debugging a web app; auth headers and cookies are masked |
| `browser_dialog`, `browser_upload(paths, ref?)`, `browser_evaluate(function)` | dialogs; uploads only from the agent's work folder; JavaScript (a script that sends requests or clicks by itself asks the owner) |
| `browser_batch(actions, stop_on_error)` | up to 20 steps in one call (type, key, wait, read): each step still goes through the gate |
| `browser_login(credential, ref, submit)` | fills a credential without the model ever seeing it (below) |

Older names (`browser_snapshot`, `browser_take_screenshot`, `browser_press_key`,
...) still work when an agent remembers them. The shared agent instructions
(`pos_worker.prompt.BROWSER_GUIDE`) teach the same habits as Claude's own
browser: text before screenshots, find then click by ref, batch predictable
steps, check after acting, stop and report at a login, CAPTCHA or 2FA it cannot
do, never type a secret.

## Reliability

- One call at a time per browser (the CLI may send several at once).
- A crashed browser (the Playwright MCP process gone, a closed page) is restarted
  within the run at the last URL (at most 3 times): a read is retried, an action
  is reported ("not known whether the step happened") so it is never done twice.
- Timeouts: an action 8 s (`BROWSER_TIMEOUT_ACTION`), a navigation 45 s, a whole
  call 90 s (`BROWSER_CALL_TIMEOUT`); a hung call restarts the browser.
- Orphaned browsers of killed runs (Chromium / Playwright MCP adopted by the
  container's init) are killed when the next browser starts; run folders older
  than 3 days are pruned.

## Live view and the trace

Every action is in the audit log with a small JPEG of the page, tied to the run:
the agent page (Activity) shows the steps as a **filmstrip** above the trace. While
the owner has the page open and the agent's run is running, its browser (or
desktop, for `tool:computer`) sends a frame every 2.5 s: **Prohlížeč živě** /
**Desktop živě** (`/api/runs/<id>/live`, polled; the guard asks
`/api/worker/browser/live` whether anyone watches and sends nothing otherwise).

## Kept logins (`browser:profile`)

The default is a fresh profile per run. For sites an agent logs into again and
again (the HA UI, later GitHub or Namecheap if the owner grants it), the owner
grants `browser:profile` (owner only). Then:

- at the start of a run the guard gets the agent's saved cookies and site storage
  (Playwright's storage state) from PersonalOS (`GET /api/worker/browser/profile`,
  only for a running run of that agent) into a private file outside the agent's
  work folder, loads it into the fresh browser and deletes the file;
- after a login, at most once a minute after actions, and at the end of the run it
  sends the state back (`PUT`); PersonalOS stores it **encrypted** (AES-GCM, a key
  per agent derived from `POS_BROWSER_PROFILE_KEY` or the session secret) in
  `data/browser-profiles/<agent id>.bin`;
- the agent's page shows **Uložená přihlášení prohlížeče** (the sites, never the
  cookies) with **Smazat přihlášení**; clearing is audited.

## Safety

- **Ú2**: page text reaches the agent wrapped as `<external trust="untrusted">`;
  the screen is untrusted data too.
- **Ú1, the outbound gate** (the owner's rule, 2026-09-27): reading, searching,
  logging in, filling in, submitting and posting are ordinary work. Paying or
  buying, signing or accepting a binding offer, deleting, account/security
  settings and posting on the owner's personal channels ask the owner first
  (Approvals page, with a screenshot); the tool waits and does nothing if
  rejected. Banking and payment sites (`pos.browser.APPROVAL_DOMAINS`,
  `POS_BROWSER_APPROVAL_DOMAINS`) ask even for a visit. The gate hears what the
  element **really** is (the guard asks the page for its role and name), not only
  the agent's description. On the desktop, the guard asks the desktop's Chromium
  (DevTools) for the page URL, the element under the pointer and the focused field.
- **Action hosts**: `scope:browser:<host[:port]>` grants (owner only). Seeded:
  Home Assistant Specialist `192.168.1.56:8123`, `homeassistant.local:8123`
  (full admin, the owner's decision); SRE `192.168.1.108`, `192.168.1.186`,
  `grafana.obseum.cloud`; Nexus Specialist `nexus(-api).obseum.cloud`;
  Knowlage Specialist `knowlage.obseum.cz`.
- **Logins**: `browser_login(credential, ref, submit)` fills a credential
  (docs/CREDENTIALS.md) into a field. PersonalOS gives the value to the guard only
  (`/api/worker/browser/credential`, with the run's credential session), only for
  a page on the credential's allowed hosts (HTTPS, or HTTP on the LAN), logged as a
  use with tool `browser`. The field is masked (`text-security: disc`) before the
  value goes in, the value is redacted (plain and encoded) from every result, and
  typed text is never stored.
- **Downloads** go to the agent's `downloads/`; programs and scripts (by
  extension and content) and files above `BROWSER_MAX_DOWNLOAD_MB` (25) go to
  `.browser/run-<id>/quarantine/`. **Uploads** only from the agent's work folder
  (not hidden folders).
- The desktop service is on its own network (only the agent pool reaches it),
  needs `DESKTOP_TOKEN` (no token: it refuses everything), runs as a non-root
  user with `no-new-privileges`, and keeps nothing between sessions.

## Grants

Seeded once at start-up (`pos.browser.ensure_grants`; a revoke stays):
`tool:browser` for the CEO, Chief of Staff, CTO, SRE, the HA, Nexus and
Knowlage specialists, Security Engineer, Head of Growth, Content & Brand, Head
of Customer Success and the CFO; `tool:computer` for the HA Specialist and the
SRE. The Access manager can grant `tool:browser` / `tool:computer` to others;
action hosts and `browser:profile` are the owner's. `agent.json` files never
grant them.

## Evaluating

`python -m pos_worker.browser_eval --agent <slug> --engine claude|codex` (inside
the agent pool) runs three tasks (read a table, a form on an internal host, a
multi-step search) through the real engine and browser as runs of that agent
and prints success, browser steps, errors, tool-result tokens, the engine's
tokens and the time per task.

## The owner's PC

A worker on the PC with `BROWSER_CDP=http://127.0.0.1:9222` drives the owner's
own Chrome (started with `--remote-debugging-port=9222`), so logged-in sites
work without PersonalOS seeing a password.

## Deploy

`docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml
--profile agents up -d --build agent-pool desktop` after setting
`POS_DESKTOP_TOKEN` (a random string) in `.env`. Optional: `POS_POOL_MEM` (4g),
`POS_DESKTOP_MEM` (1g), `POS_BROWSER_MAX_CONCURRENT` (3), `POS_BROWSER_MAX_MB`
(1200), `POS_BROWSER_MAX_SCREENSHOTS` (15), `POS_BROWSER_PROFILE_KEY` (the kept
logins' key; default: the session secret). The api restart seeds the grants.
Codex in the pool needs its own login (`/codex`, `CODEX_HOME_HOST`).
