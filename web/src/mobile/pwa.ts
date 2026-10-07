import { useEffect, useState } from "react";
import { api } from "../api";

/*
 * The installed app's plumbing (docs/MOBILE.md): the service worker and its updates,
 * the browser's install prompt, and Web Push on this device.
 */

type InstallPrompt = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };

let registration: ServiceWorkerRegistration | null = null;
let waiting: ServiceWorker | null = null;
let installEvent: InstallPrompt | null = null;
const emit = (name: string, detail?: unknown) => window.dispatchEvent(new CustomEvent(name, { detail }));

export const isStandalone = () =>
  window.matchMedia("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;

/** Register /sw.js for /m; a new deploy's worker waits and the app offers "Nová verze, obnovit". */
export function registerServiceWorker() {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installEvent = e as InstallPrompt;
    emit("pos:installable");
  });
  window.addEventListener("appinstalled", () => {
    installEvent = null;
    emit("pos:installable");
  });
  if (!("serviceWorker" in navigator)) return;
  // A click on a notification while the app is open: go to that conversation or item.
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data?.type === "open" && typeof e.data.url === "string") emit("pos:open", e.data.url);
  });
  let reloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloading) return;
    reloading = true;
    window.location.reload();
  });
  navigator.serviceWorker
    .register("/sw.js", { scope: "/m" })
    .then((reg) => {
      registration = reg;
      const found = (w: ServiceWorker | null) => {
        if (!w) return;
        const ready = () => {
          // Only an update (a worker already controls the page) needs a reload; the first install does not.
          if (w.state === "installed" && navigator.serviceWorker.controller) {
            waiting = w;
            emit("pos:update");
          }
        };
        w.addEventListener("statechange", ready);
        ready();
      };
      found(reg.waiting);
      reg.addEventListener("updatefound", () => found(reg.installing));
      // An app left open for days still hears about deploys: check when it comes back to the front.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") reg.update().catch(() => undefined);
        // Silent path: the new version waits and the app went to the background: take it now.
        else if (waiting) applyUpdate();
      });
    })
    .catch(() => undefined);
}

export function applyUpdate() {
  if (waiting) waiting.postMessage("skip-waiting");
  else window.location.reload();
}

export function useUpdateReady(): boolean {
  const [ready, setReady] = useState(!!waiting);
  useEffect(() => {
    const on = () => setReady(true);
    window.addEventListener("pos:update", on);
    return () => window.removeEventListener("pos:update", on);
  }, []);
  return ready;
}

export function useInstallable(): boolean {
  const [can, setCan] = useState(!!installEvent);
  useEffect(() => {
    const on = () => setCan(!!installEvent);
    window.addEventListener("pos:installable", on);
    return () => window.removeEventListener("pos:installable", on);
  }, []);
  return can;
}

export async function promptInstall() {
  if (!installEvent) return;
  await installEvent.prompt();
  await installEvent.userChoice.catch(() => undefined);
  installEvent = null;
  emit("pos:installable");
}

/* ------------------------------------------------------------------ push */

export type PushPrefs = {
  chat: boolean;
  needs: boolean;
  urgent: boolean;
  quiet: boolean;
  quiet_from: string;
  quiet_to: string;
  preview: boolean;
};
export type PushConfig = { enabled: boolean; public_key: string | null; prefs: PushPrefs; devices: number; this_device?: boolean };
export type PushTestResult = {
  sub_id: number;
  device_id: string | null;
  label: string;
  host: string;
  status: number | null;
  ok: boolean;
  error: string | null;
  removed: boolean;
  last_seen_at: string | null;
};

export const pushApi = {
  config: () => api<PushConfig>("/api/push/config"),
  prefs: (changes: Partial<PushPrefs>) => api<PushPrefs>("/api/push/prefs", { method: "PUT", body: JSON.stringify(changes) }),
  test: () => api<{ sent: number }>("/api/push/test", { method: "POST" }),
  /** "Poslat testovací notifikaci": a labelled test to each subscribed device, the result per device. */
  testAll: () => api<{ sent: number; devices: number; results: PushTestResult[] }>("/api/push/test", { method: "POST", body: JSON.stringify({ all: true }) }),
};

export const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
/** iPhone or iPad (iPadOS reports itself as a Mac with touch). */
export const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.userAgent.includes("Macintosh") && navigator.maxTouchPoints > 1);
export const isAndroid = () => /Android/.test(navigator.userAgent);

function keyBytes(b64: string): Uint8Array<ArrayBuffer> {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4));
  const out = new Uint8Array(new ArrayBuffer(s.length));
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

/** A failure on the way to a subscription goes to the server (pos.push.client_error), never only to a toast:
 * on 29. 9. the owner's phone failed here and nothing of it was known. */
export function reportPushError(step: string, e: unknown) {
  const error = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
  api("/api/push/client-error", { method: "POST", body: JSON.stringify({ step, error: error.slice(0, 290), standalone: isStandalone() }) }).catch(() => undefined);
}

async function step<T>(name: string, message: string, fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    reportPushError(name, e);
    throw new Error(`${message} (${e instanceof Error ? e.message || e.name : String(e)})`);
  }
}

/** The /m service worker's registration once it is active. `navigator.serviceWorker.ready` never settles on a
 * page outside its scope (Nastavení → Mobilní aplikace on the desktop is "/settings/…"), so wait on the
 * registration itself, with a timeout. */
async function pushRegistration(): Promise<ServiceWorkerRegistration> {
  const reg =
    registration ?? (await navigator.serviceWorker.getRegistration("/m")) ?? (await navigator.serviceWorker.register("/sw.js", { scope: "/m" }));
  if (reg.active) return reg;
  const worker = reg.installing ?? reg.waiting;
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("service worker did not activate in 15 s")), 15000);
    const done = () => {
      if (reg.active) {
        window.clearTimeout(timer);
        resolve();
      }
    };
    worker?.addEventListener("statechange", done);
    done();
  });
  return reg;
}

/** This device's subscription, if any. */
export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/m");
  return (await reg?.pushManager.getSubscription()) ?? null;
}

const postSubscription = (sub: PushSubscription) => api("/api/push/subscribe", { method: "POST", body: JSON.stringify(sub.toJSON()) });

/** "Zapnout oznámení" (a click: iOS and Chrome ask for the permission only from a user gesture). Each step that
 * fails says which one and is reported to the server. */
export async function enablePush(publicKey: string): Promise<void> {
  if (isIOS() && !isStandalone()) throw new Error("Na iPhonu fungují oznámení jen v aplikaci přidané na plochu: Sdílet → Přidat na plochu, pak ji otevři z plochy.");
  // requestPermission first and directly from the click: Safari refuses it after another await.
  const permission = await step("permission", "Oprávnění k oznámením se nepodařilo získat", () => Notification.requestPermission());
  if (permission !== "granted") {
    reportPushError("permission", new Error(permission));
    throw new Error(
      permission === "denied"
        ? "Oznámení jsou zakázaná. Povol je v nastavení telefonu (Android: Nastavení → Aplikace → PersonalOS nebo Chrome → Oznámení) a zkus to znovu."
        : "Oznámení nebyla povolena (dialog zavřen). Zkus to znovu a zvol Povolit.",
    );
  }
  const reg = await step("service_worker", "Aplikace se nepřipravila (service worker)", pushRegistration);
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await step("subscribe", "Služba oznámení prohlížeče odmítla přihlášení", () =>
      reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) }),
    );
  }
  await step("server", "Server přihlášení k oznámením nepřijal", () => postSubscription(sub));
}

/** "Zapnout notifikace v telefonu" on the owner's item in "Čeká na tebe": the permission straight from the click
 * (Safari refuses it after another await), then the server's key, then the same steps as Settings. */
export async function enablePushFromClick(): Promise<void> {
  if (!pushSupported()) throw new Error("Tady oznámení zapnout nejdou. Otevři PersonalOS v telefonu (/m) a stiskni to tam.");
  if (isIOS() && !isStandalone()) throw new Error("Na iPhonu fungují oznámení jen v aplikaci přidané na plochu: Sdílet → Přidat na plochu, pak ji otevři z plochy.");
  await Notification.requestPermission();
  const cfg = await pushApi.config();
  if (!cfg.enabled || !cfg.public_key) throw new Error("Oznámení nejsou na serveru zapnutá.");
  await enablePush(cfg.public_key);
}

/** On every start of the app (and on opening Settings): a subscription the browser has but the server does not
 * (a failed POST, a subscription the server dropped) is registered again; with the permission granted and no
 * subscription at all, one is made (no prompt is needed then). Silent; failures are reported. */
export async function syncPush(cfg?: PushConfig): Promise<boolean> {
  if (!pushSupported() || Notification.permission !== "granted") return false;
  try {
    const c = cfg ?? (await pushApi.config());
    if (!c.enabled || !c.public_key) return false;
    const reg = await pushRegistration();
    let sub = await reg.pushManager.getSubscription();
    if (sub && c.this_device) return false;
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(c.public_key) });
    await postSubscription(sub);
    return true;
  } catch (e) {
    reportPushError("sync", e);
    return false;
  }
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await api("/api/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint: sub.endpoint }) }).catch(() => undefined);
  await sub.unsubscribe();
}

/** The app icon's badge: what waits for you (Android and desktop where supported). */
export function setBadge(n: number) {
  const nav = navigator as Navigator & { setAppBadge?: (n?: number) => Promise<void>; clearAppBadge?: () => Promise<void> };
  if (n > 0) nav.setAppBadge?.(n).catch(() => undefined);
  else nav.clearAppBadge?.().catch(() => undefined);
}
