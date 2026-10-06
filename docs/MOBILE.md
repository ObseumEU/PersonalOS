# Mobile app (PWA)

PersonalOS on the phone and as a desktop app: a trimmed app at
**https://personalos.obseum.cz/m**, installed from the browser. No app store,
no Google developer account, no APK, no cable. It updates itself on every
deploy. It works only at home or over the VPN, like the rest of the site.

## Install

**Android (Chrome)**
1. On the VPN (or home Wi-Fi), open `https://personalos.obseum.cz/m` in Chrome
   (or scan the QR code in Nastavení → Mobilní aplikace).
2. Menu ⋮ → **Nainstalovat aplikaci** (older Chrome: "Přidat na plochu").
3. Open PersonalOS from the home screen, sign in once, then Víc → Nastavení →
   **Zapnout oznámení** and allow them.

**PC (Chrome or Edge)**: open `https://personalos.obseum.cz/m`, click the install
icon at the right of the address bar (or menu → "Nainstalovat PersonalOS").

**Updates** are automatic: every deploy builds a new service worker; an open app
shows "Nová verze · Obnovit", and an app in the background takes the new
version silently and has it on the next launch.

## What is in it

- **Chat** first: the CEO pinned, then agents and people (unread badges, "píše…",
  "pracuje na T-12"), channels, #system folded. A conversation is full screen with
  threads, live typing, photo/file attachments (uploaded to Soubory and linked in
  the message) and actions on a message: reply in thread, **Vytvoř úkol**, **Schval**
  (decides the approval the message names, `schválení #N`, else replies "Schvaluji"),
  👍, copy.
- **Čeká na tebe** with inline Schválit / Zamítnout / Odpovědět / Vrátit, and **Otevřít prohlížeč** for an
  agent's live browser waiting for one step of yours (`/m/handoff/<id>`, also straight from the push): tap into
  the page, type in the field under it, Enter / Tab / scroll / zoom buttons, then **Hotovo** (docs/BROWSER.md,
  "Předání majiteli"). Loaded on first use, outside the /m budget.
- **Úkoly**: Dnes, Další, Agenti, Čeká, K revizi; quick capture; a tap opens the
  full task panel (loaded on demand).
- **Víc**: the settings below and a link to the full app.

Code: `web/m.html`, `web/src/mobile/`, the service worker `web/sw/sw.js` (built by
`web/pwa-plugin.js` with this deploy's version and precache list), the manifest
`web/public/manifest.webmanifest`, icons `web/public/icons/`. The /m entry has a
size budget (150 KB gzip for the entry and its static imports), checked on every
build by `web/scripts/check-pwa.mjs` together with the manifest and sw.js.

## Notifications (Web Push)

`pos.push` sends through the browsers' push services (FCM for Chrome, Mozilla,
Apple, Windows) with VAPID; the payload is only a title, a short redacted
preview and the `/m` address to open. A click opens that exact conversation,
thread or item.

What notifies (every 10 s, per member with a subscribed device):

| Category | What | Quiet hours |
|---|---|---|
| Zprávy pro mě | a DM to you; in a group a message that @mentions you or answers your thread | held |
| Čeká na tebe | a new approval, ask, result to review, customer draft ready | waits for the morning (one summary if many) |
| Naléhavé | an agent's message with priority stop / change_plan (the CEO's urgent ones), a blocking ask | always |

Never: #system notices, what you already read, chat pings that only announce an
ask or approval (the item itself notifies). One notification per conversation
(tag), a quick follow-up updates it silently. Settings per member in the app:
each category on/off, text preview on/off, quiet hours (default 22:00–07:00).
A subscription the push service reports gone (404/410) is deleted.

**Keys**: `POS_VAPID_PRIVATE_KEY` / `POS_VAPID_PUBLIC_KEY` (base64url) and
`POS_VAPID_SUBJECT` in the server's `.env`. Generate them once on the server,
without printing:

```bash
cd /opt/server/personalos/app
grep -q '^POS_VAPID_PRIVATE_KEY=' .env || docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml \
  run --rm --no-deps -T api python -c "from pos.push import generate_vapid; p, q = generate_vapid(); print(f'POS_VAPID_PRIVATE_KEY={p}'); print(f'POS_VAPID_PUBLIC_KEY={q}')" >> .env
```

Without the keys push is off (the app says so); everything else works.

## Security

- The same session cookie as the web (HttpOnly, Secure, SameSite=Lax). Every
  login is a **device** (`pos.devices`): listed in Nastavení → Mobilní aplikace →
  Přihlášená zařízení and revocable there (its session ends and its push
  subscriptions go). A browser stays signed in for 30 days of no use, the
  installed app for a year of no use; logout ends the device.
- The server posts pushes only to known push-service hosts (no SSRF through a
  subscription). No secrets in payloads: previews drop code blocks and key-like
  strings, and can be turned off.
- `/m` is served with a strict Content-Security-Policy (`web/nginx.conf`).
- The site answers only the LAN and the VPN (front proxy); outside, the app shows
  "Jsi offline nebo mimo VPN" with a retry (the cached shell opens offline).

## TLS

Installable apps and service workers need a trusted certificate. Caddy on svr03
serves **personalos.obseum.cz with a public Let's Encrypt certificate** (checked
2026-09-29: issuer Let's Encrypt, valid to 2026-12-24), obtained through the VPS
tunnel (`deploy/prod/front-proxy.Caddyfile`), so phones trust it with no setup.
If that path ever breaks (renewal needs the ACME challenge to reach Caddy
through 23.88.52.230), switch the site to the DNS-01 challenge with a DNS API
token for obseum.cz; an internal CA would make phones reject the app.
