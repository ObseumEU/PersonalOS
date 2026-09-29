/* PersonalOS service worker for the installed app (/m). docs/MOBILE.md.
 *
 * - The app shell (/m and its hashed files) is precached at install; every
 *   deploy builds a new sw.js (a new VERSION and file list), so the browser
 *   installs it, and the open app offers "Nová verze, obnovit".
 * - Navigations under /m: network first; offline or outside the VPN (the
 *   public address answers 403) the cached shell opens and shows its offline
 *   screen with a retry. No shell yet: a small offline page.
 * - /api: always the network, never cached (fresh data, no stale secrets).
 * - /assets (hashed, immutable): cache first, filled as they are used.
 * - Push: a title, a short text and the /m address; a newer message of the
 *   same conversation replaces the older notification (tag). A click opens the
 *   exact conversation or item in the app.
 *
 * The build (vite.config.ts, pwaPlugin) fills VERSION and PRECACHE.
 */
const VERSION = "__VERSION__";
const PRECACHE = __PRECACHE__;
const SHELL = "/m.html";
const CACHE = `pos-shell-${VERSION}`;
const ASSETS = "pos-assets";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(PRECACHE.map((u) => new Request(u, { cache: "reload" }))))
      .catch(() => undefined),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith("pos-shell-") && k !== CACHE).map((k) => caches.delete(k)));
      // Hashed files of old deploys: dropped once the new shell no longer needs them.
      const assets = await caches.open(ASSETS);
      const keep = new Set(PRECACHE.map((u) => new URL(u, self.location.origin).pathname));
      for (const req of await assets.keys()) {
        if (!keep.has(new URL(req.url).pathname)) await assets.delete(req);
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

const OFFLINE_HTML = `<!doctype html><html lang="cs"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PersonalOS · offline</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0d10;color:#e6e8eb;font:15px system-ui,sans-serif;text-align:center}
main{padding:24px;max-width:320px}h1{font-size:18px;font-weight:500}p{color:#a3a9b3;line-height:1.5}button{margin-top:12px;height:44px;padding:0 20px;border-radius:6px;border:1px solid #6cc4dc;background:rgba(108,196,220,.1);color:#6cc4dc;font-size:15px}</style></head>
<body><main><h1>Jsi offline nebo mimo VPN</h1><p>PersonalOS je dostupný jen z domácí sítě a přes VPN. Zapni VPN a zkus to znovu.</p><button onclick="location.reload()">Zkusit znovu</button></main></body></html>`;

async function shell() {
  const hit = (await caches.match(SHELL)) || (await caches.match("/m"));
  return hit || new Response(OFFLINE_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/mcp") return; // the network, as is

  if (req.mode === "navigate") {
    if (!(url.pathname === "/m" || url.pathname.startsWith("/m/"))) return;
    event.respondWith(
      (async () => {
        try {
          const res = await fetch(req, { cache: "no-store" });
          // Outside the VPN the public proxy answers 403: that is "offline" for this app.
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(SHELL, copy)).catch(() => undefined);
            return res;
          }
          return shell();
        } catch {
          return shell();
        }
      })(),
    );
    return;
  }

  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/icons/")) {
    event.respondWith(
      (async () => {
        const hit = await caches.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) {
          const copy = res.clone();
          caches.open(ASSETS).then((c) => c.put(req, copy)).catch(() => undefined);
        }
        return res;
      })(),
    );
  }
});

/* ------------------------------------------------------------------ push */

self.addEventListener("push", (event) => {
  let p = {};
  try {
    p = event.data ? event.data.json() : {};
  } catch {
    p = { title: "PersonalOS", body: event.data ? event.data.text() : "" };
  }
  const title = p.title || "PersonalOS";
  event.waitUntil(
    (async () => {
      // The app is open and looking at exactly this conversation: no notification needed.
      const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const url = p.url || "/m";
      if (wins.some((w) => w.visibilityState === "visible" && w.focused && new URL(w.url).pathname + new URL(w.url).search === url)) return;
      await self.registration.showNotification(title, {
        body: p.body || "",
        tag: p.tag || undefined,
        renotify: !!p.tag && !p.silent,
        silent: !!p.silent,
        icon: "/icons/icon-192.png",
        badge: "/icons/badge-96.png",
        timestamp: p.ts ? Date.parse(p.ts) : Date.now(),
        requireInteraction: p.kind === "urgent",
        data: { url },
      });
      if (navigator.setAppBadge) navigator.setAppBadge().catch(() => undefined);
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/m";
  event.waitUntil(
    (async () => {
      const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const app = wins.find((w) => new URL(w.url).pathname.startsWith("/m"));
      if (app) {
        await app.focus();
        app.postMessage({ type: "open", url });
        return;
      }
      await self.clients.openWindow(url);
    })(),
  );
});

// The browser renewed the subscription: tell the server (it keeps one row per endpoint).
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      const old = event.oldSubscription;
      const cfg = await fetch("/api/push/config", { credentials: "same-origin" }).then((r) => r.json());
      if (!cfg.public_key) return;
      const key = Uint8Array.from(atob(cfg.public_key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((cfg.public_key.length + 3) % 4)), (c) => c.charCodeAt(0));
      const sub = event.newSubscription || (await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }));
      await fetch("/api/push/subscribe", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sub.toJSON()) });
      if (old) await fetch("/api/push/unsubscribe", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: old.endpoint }) });
    })(),
  );
});
