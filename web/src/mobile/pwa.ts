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
export type PushConfig = { enabled: boolean; public_key: string | null; prefs: PushPrefs; devices: number };

export const pushApi = {
  config: () => api<PushConfig>("/api/push/config"),
  prefs: (changes: Partial<PushPrefs>) => api<PushPrefs>("/api/push/prefs", { method: "PUT", body: JSON.stringify(changes) }),
  test: () => api<{ sent: number }>("/api/push/test", { method: "POST" }),
};

export const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function keyBytes(b64: string): Uint8Array<ArrayBuffer> {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (b64.length % 4)) % 4));
  const out = new Uint8Array(new ArrayBuffer(s.length));
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

async function pushRegistration(): Promise<ServiceWorkerRegistration> {
  if (registration) return registration;
  // The full app (Nastavení → Mobilní aplikace) has no worker of its own: use the app's.
  return (await navigator.serviceWorker.getRegistration("/m")) ?? navigator.serviceWorker.register("/sw.js", { scope: "/m" });
}

/** This device's subscription, if any. */
export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/m");
  return (await reg?.pushManager.getSubscription()) ?? null;
}

export async function enablePush(publicKey: string): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error(permission);
  const reg = await pushRegistration();
  await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
  await api("/api/push/subscribe", { method: "POST", body: JSON.stringify(sub.toJSON()) });
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
