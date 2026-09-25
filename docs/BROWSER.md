# Browser use

Agents with the `browser:use` permission get a real web browser as the MCP
server `browser`: [Playwright MCP](https://github.com/microsoft/playwright-mcp)
behind PersonalOS's guard (`worker/pos_worker/browser_guard.py`).

## Where the browser runs

- **Server (default):** a headless Chromium per agent session, isolated profile.
  Public pages, research, forms, screenshots.
- **The owner's PC:** a worker on the PC with `BROWSER_CDP=http://127.0.0.1:9222`
  drives the owner's own Chrome (started with `--remote-debugging-port=9222`),
  so logged-in sites (Gmail web, admin consoles) work without PersonalOS ever
  seeing a password. Give that agent `browser:use` and assign it the tasks that
  need those logins; other agents hand work to it (handoff_task, chat).

## Rules (owner's decision, 2026-09-25)

No approval for browsing, reading, logged-in pages or filling in forms.
Approval (the Approvals page, with a screenshot) only before:

- paying, buying, subscribing or transferring money;
- sending a message or e-mail on the owner's behalf (a Send button, Enter in a
  message field);
- deleting things, closing accounts;
- changing passwords, two-factor or other security settings;
- any page on a banking or payment site (`pos.browser.APPROVAL_DOMAINS`, more
  with `POS_BROWSER_APPROVAL_DOMAINS`);
- a script that sends requests or clicks by itself (`browser_evaluate`).

`BROWSER_ALLOW` (optional, per worker) lists an agent's usual sites; others then
ask first too. Every action is in the audit log with a screenshot
(`data/files/browser/`); typed text and form values are never stored. Page
content reaches the agent wrapped as untrusted external data. The kill switch
and pausing an agent stop its browser at the next action, and a session ends
after `BROWSER_MAX_MINUTES` (30).
