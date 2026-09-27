# Browser use and computer use

Agents get a real web browser, and for tasks that need a real GUI a desktop,
like Claude has. Both are MCP servers the worker mounts into a run **only with
the grant**; both go through the same PersonalOS guard.

| | Browser (`tool:browser`, older `browser:use`) | Computer (`tool:computer`) |
|---|---|---|
| What | [Playwright MCP](https://github.com/microsoft/playwright-mcp) (`@playwright/mcp`), headless Chromium, behind `worker/pos_worker/browser_guard.py` | the desktop sandbox `ops/desktop` (Xvfb + fluxbox + Chromium + xdotool + ImageMagick) behind `worker/pos_worker/computer.py` |
| Tools | `browser_navigate`, `browser_snapshot` (accessibility tree, cheap), `browser_click`, `browser_type`, `browser_fill_form`, `browser_take_screenshot`, ... and `browser_login` | `computer_screenshot`, `computer_zoom`, `computer_left_click` (+ right, middle, double, triple), `computer_left_click_drag`, `computer_mouse_move`, `computer_scroll`, `computer_type`, `computer_key`, `computer_wait`, `computer_open_url`, `computer_cursor_position` |
| Starts | on the run's first browser call (the tool list is cached) | on the first `computer_*` call |
| Ends | with the run (the CLI closes the MCP server) or after `BROWSER_MAX_MINUTES` (30) | with the run, or after `DESKTOP_IDLE_S` (300) idle / `DESKTOP_MAX_S` (1800) |
| Isolation | a fresh in-memory profile per run (`--isolated`); a persistent one only with `scope:browser-profile:<name>` (kept in the agent's `.browser-profiles/<name>`) | a fresh profile per session in tmpfs, deleted with it |
| Memory | Chromium flags (`--js-flags=--max-old-space-size=256`, 2 renderers); at most `BROWSER_MAX_CONCURRENT` (2) browsers in the pool at once, the others wait for a slot; one above `BROWSER_MAX_MB` (700) is closed | idle ~15 MB, a session ~220 MB; container `mem_limit` 1 GB; **one session at a time**, the next run waits in line (`DESKTOP_QUEUE_WAIT`, 600 s) |

Claude Code's CLI has no native computer use tool (Anthropic's API tool,
`computer_toolset_20260801`, GA for claude-opus-5-5 and newer, is for
API agent loops), so the `computer` MCP server offers the same actions under
`computer_<action>`. Codex gets both servers too (`-c mcp_servers...`).

## Safety

- **Ú2**: page text reaches the agent wrapped as `<external trust="untrusted">`;
  the screen is untrusted data too.
- **Ú1, the outbound gate** (the owner's rule, 2026-09-27): reading, searching,
  logging in and filling in need no approval. *Submitting* (a submit/send/post/
  save button, Enter in a form field, a file upload) on a site that is not one
  of the agent's **action hosts** asks the owner first (Approvals page, with a
  screenshot); the tool waits, and does nothing if rejected. Paying, sending
  messages, deleting and account/security settings ask always, banking and
  payment sites (`pos.browser.APPROVAL_DOMAINS`, `POS_BROWSER_APPROVAL_DOMAINS`)
  even for a visit. On the desktop, the guard asks the desktop's Chromium
  (DevTools, inside the container) for the page URL, the element under the
  pointer and the focused field, and applies the same rule.
- **Action hosts**: `scope:browser:<host[:port]>` grants (owner only). Seeded:
  Home Assistant Specialist `192.168.1.56:8123`, `homeassistant.local:8123`
  (full admin, the owner's decision); SRE `192.168.1.108`, `192.168.1.186`,
  `grafana.obseum.cloud`; Nexus Specialist `nexus(-api).obseum.cloud`;
  Knowlage Specialist `knowlage.obseum.cz`.
- **Logins**: `browser_login(credential, element, ref, submit)` fills a
  credential (docs/CREDENTIALS.md) into a field. PersonalOS gives the value to
  the guard only (`/api/worker/browser/credential`, with the run's credential
  session), only for a page on the credential's allowed hosts (HTTPS, or HTTP
  on the LAN), logged as a use with tool `browser`. The value is redacted
  (plain and encoded) from every snapshot and result, the field is masked
  (`text-security: disc`) for screenshots, and typed text is never stored.
- **Downloads** go to the agent's `downloads/`; programs and scripts (by
  extension and content) and files above `BROWSER_MAX_DOWNLOAD_MB` (25) go to
  `.browser/run-<id>/quarantine/`.
- **Cost**: prefer `browser_snapshot`; screenshots are capped per run
  (`BROWSER_MAX_SCREENSHOTS` 10, `DESKTOP_MAX_SCREENSHOTS` 40).
- **Trail**: every action is in the audit log with a screenshot
  (`data/files/browser/`), tied to the run, so the agent page's trace shows it
  with a `[screenshot]` link.
- The desktop service is on its own network (only the agent pool reaches it),
  needs `DESKTOP_TOKEN` (no token: it refuses everything), runs as a non-root
  user with `no-new-privileges`, and keeps nothing between sessions.

## Grants

Seeded once at start-up (`pos.browser.ensure_grants`; a revoke stays):
`tool:browser` for the CEO, Chief of Staff, CTO, SRE, the HA, Nexus and
Knowlage specialists, Security Engineer, Head of Growth, Content & Brand, Head
of Customer Success and the CFO; `tool:computer` for the HA Specialist and the
SRE. The Access manager can grant `tool:browser` / `tool:computer` to others;
action hosts and persistent profiles are the owner's. `agent.json` files never
grant them.

## The owner's PC

A worker on the PC with `BROWSER_CDP=http://127.0.0.1:9222` drives the owner's
own Chrome (started with `--remote-debugging-port=9222`), so logged-in sites
work without PersonalOS seeing a password.

## Deploy

`docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml
--profile agents up -d --build agent-pool desktop` after setting
`POS_DESKTOP_TOKEN` (a random string) in `.env`. Optional: `POS_POOL_MEM` (3g),
`POS_DESKTOP_MEM` (1g), `POS_BROWSER_MAX_CONCURRENT`, `POS_BROWSER_MAX_MB`,
`POS_BROWSER_MAX_SCREENSHOTS`. The api restart seeds the grants.
