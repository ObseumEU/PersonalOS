import { useEffect, useState } from "react";
import { api } from "../api";
import { isPushServiceError, pushServiceMessage, subscribeWithRetry } from "./pushSubscribe";

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
export type PushServerStatus = {
  subscription: { id: number; created_at: string; last_ok_at: string | null; last_error: string | null; failures: number; host: string } | null;
  last_ok_at: string | null;
  last_failure: { at: string; status: number | null; error: string; host: string } | null;
  steps: { at: string; step: string; ok: boolean; detail: string }[];
};
export type PushConfig = {
  enabled: boolean;
  public_key: string | null;
  prefs: PushPrefs;
  devices: number;
  this_device?: boolean;
  status?: PushServerStatus;
};
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

/** One step of turning notifications on, as shown in Settings and sent to the server (push_client_log). */
export type PushStep = { step: string; ok: boolean; detail: string };

/** Every step of the subscribe flow goes to the server, successes too (POST /api/push/client-log; a failed step is
 * also a client failure in push_failures): on 29. 9. and again on 13. 10. the owner's phone failed here and the
 * server knew nothing of it. */
export function logPushStep(step: string, ok: boolean, detail: string) {
  api("/api/push/client-log", { method: "POST", body: JSON.stringify({ step, ok, detail: detail.slice(0, 290), standalone: isStandalone() }) }).catch(
    () => undefined,
  );
}

const errDetail = (e: unknown) => (e instanceof Error ? `${e.name}: ${e.message}` : String(e));

export function reportPushError(step: string, e: unknown) {
  logPushStep(step, false, errDetail(e));
}

/** The flow stopped at `step`; `steps` are all the steps done so far (the last one failed). */
export class PushFlowError extends Error {
  step: string;
  steps: PushStep[];
  constructor(step: string, message: string, steps: PushStep[]) {
    super(message);
    this.step = step;
    this.steps = steps;
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

/** Settings → "Stav notifikací", the browser's half. */
export type PushDiag = { supported: boolean; permission: string; standalone: boolean; sw: string; subscription: string | null; keyMatches: boolean | null };

export async function pushDiagnostics(publicKey?: string | null): Promise<PushDiag> {
  const supported = pushSupported();
  const d: PushDiag = {
    supported,
    permission: "Notification" in window ? Notification.permission : "nepodporováno",
    standalone: isStandalone(),
    sw: "nepodporován",
    subscription: null,
    keyMatches: null,
  };
  if (!("serviceWorker" in navigator)) return d;
  try {
    const reg = await navigator.serviceWorker.getRegistration("/m");
    d.sw = !reg ? "neregistrován" : reg.active ? `aktivní (${new URL(reg.scope).pathname})` : reg.installing || reg.waiting ? "instaluje se" : "neaktivní";
    const sub = supported ? await reg?.pushManager.getSubscription() : null;
    if (sub) {
      d.subscription = new URL(sub.endpoint).host;
      d.keyMatches = publicKey ? sameKey(sub, publicKey) : null;
    }
  } catch (e) {
    d.sw = `chyba: ${errDetail(e)}`;
  }
  return d;
}

function sameKey(sub: PushSubscription, publicKey: string): boolean {
  const have = sub.options?.applicationServerKey;
  if (!have) return true; // the browser does not say: assume it is ours
  const a = new Uint8Array(have);
  const b = keyBytes(publicKey);
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** The push service's subscribe, bounded: on a phone without a connection to it (FCM blocked, no Google Play
 * services) it can stay pending for ever and the button would say "Zapínám…" with nothing reported. */
function subscribeTimed(reg: ServiceWorkerRegistration, key: string, ms = 30000): Promise<{ sub: PushSubscription; retried: boolean }> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(`push service did not answer in ${ms / 1000} s`)), ms);
    subscribeWithRetry(reg.pushManager, { userVisibleOnly: true, applicationServerKey: keyBytes(key) }).then(
      (s) => {
        window.clearTimeout(timer);
        resolve(s);
      },
      (e) => {
        window.clearTimeout(timer);
        reject(e);
      },
    );
  });
}

const postSubscription = (sub: PushSubscription) => api("/api/push/subscribe", { method: "POST", body: JSON.stringify(sub.toJSON()) });

const NO_PROMPT_MS = 1500;

function permissionError(permission: NotificationPermission, ms: number): string {
  if (permission === "denied")
    return isAndroid()
      ? "Oznámení jsou zakázaná. Android: podrž ikonu PersonalOS → Informace o aplikaci → Oznámení → Povolit (případně Nastavení → Aplikace → Chrome → Oznámení), pak aplikaci otevři znovu a stiskni Zapnout notifikace."
      : "Oznámení jsou v prohlížeči zakázaná. Povol je v nastavení webu (ikona zámku vedle adresy) a zkus to znovu.";
  if (ms < NO_PROMPT_MS && isAndroid())
    return (
      "Telefon dotaz na oznámení vůbec nezobrazil: Android má oznámení pro Chrome nebo pro aplikaci PersonalOS vypnutá. " +
      "Zapni je: Nastavení → Aplikace → Chrome → Oznámení → Povolit (a stejně u aplikace PersonalOS, pokud tam je). " +
      "Pak PersonalOS otevři znovu a stiskni Zapnout notifikace."
    );
  if (ms < NO_PROMPT_MS) return "Prohlížeč dotaz na oznámení nezobrazil (ztlumil ho). Povol oznámení v nastavení webu (ikona zámku vedle adresy) a zkus to znovu.";
  return "Oznámení nebyla povolena (dialog zavřen). Zkus to znovu a zvol Povolit.";
}

/** "Zapnout notifikace" (and "Přihlásit toto zařízení"): the whole flow on every click, nothing remembered from an
 * earlier attempt. The permission is asked first and straight from the click (iOS and Chrome ask only from a user
 * gesture; Safari refuses it after another await); already granted, it is not asked and the device subscribes at
 * once. Then the server's key, the service worker, the push service's subscription (renewed when it was made with
 * another key) and the server. Each step's result is sent to the server and returned; a failed one throws
 * PushFlowError naming the step. */
export async function enablePush(publicKey?: string | null): Promise<PushStep[]> {
  const steps: PushStep[] = [];
  const done = (step: string, detail: string) => {
    steps.push({ step, ok: true, detail });
    logPushStep(step, true, detail);
  };
  const fail = (step: string, message: string, detail: string): never => {
    steps.push({ step, ok: false, detail: detail && detail !== message ? `${message} (${detail})` : message });
    logPushStep(step, false, detail || message);
    throw new PushFlowError(step, message, steps);
  };
  const run = async <T>(step: string, message: string, fn: () => Promise<T>): Promise<T> => {
    try {
      return await fn();
    } catch (e) {
      return fail(step, message, errDetail(e));
    }
  };

  if (!pushSupported()) {
    const missing = [!("serviceWorker" in navigator) && "serviceWorker", !("PushManager" in window) && "PushManager", !("Notification" in window) && "Notification"];
    fail("support", isIOS() ? "Na iPhonu fungují oznámení jen v aplikaci přidané na plochu." : "Tento prohlížeč oznámení z webu neumí.",
      `${missing.filter(Boolean).join(", ")} missing`);
  }
  if (isIOS() && !isStandalone()) fail("support", "Na iPhonu fungují oznámení jen v aplikaci přidané na plochu: Sdílet → Přidat na plochu, pak ji otevři z plochy.", "iOS, not standalone");

  const before = Notification.permission;
  if (before === "granted") done("permission", "granted (already)");
  else {
    const t0 = performance.now();
    const permission = await run("permission", "Oprávnění k oznámením se nepodařilo získat", () => Notification.requestPermission());
    const ms = Math.round(performance.now() - t0);
    const detail = `${before} → ${permission} (${ms} ms${permission === "default" && ms < NO_PROMPT_MS ? ", no prompt shown" : ""})`;
    if (permission !== "granted") fail("permission", permissionError(permission, ms), detail);
    done("permission", detail);
  }

  let key = publicKey ?? null;
  if (!key) {
    const cfg = await run("key", "Nepodařilo se načíst nastavení oznámení ze serveru", () => pushApi.config());
    if (!cfg.enabled || !cfg.public_key) fail("key", "Oznámení nejsou na serveru zapnutá (chybí klíče VAPID).", "server: push off");
    key = cfg.public_key;
  }
  const vapid = key as string;
  done("key", `VAPID public key, ${vapid.length} chars`);

  const reg = await run("service_worker", "Aplikace se nepřipravila (service worker)", pushRegistration);
  done("service_worker", `active, scope ${new URL(reg.scope).pathname}`);

  let sub = await run("subscribe", "Nepodařilo se zjistit přihlášení k oznámením", () => reg.pushManager.getSubscription());
  let renewed = "";
  if (sub && !sameKey(sub, vapid)) {
    // Made with another server key (the keys changed): the push service would refuse our sends. Make a new one.
    await sub.unsubscribe().catch(() => undefined);
    sub = null;
    renewed = "old subscription had another key, renewed; ";
  }
  if (!sub) {
    let made: { sub: PushSubscription; retried: boolean };
    try {
      made = await subscribeTimed(reg, vapid);
    } catch (e) {
      const brave = "brave" in navigator;
      return fail("subscribe", isPushServiceError(e) ? pushServiceMessage(brave) : "Služba oznámení prohlížeče odmítla přihlášení",
        `${errDetail(e)}${isPushServiceError(e) ? ", retried once" : ""}${brave ? ", Brave" : ""}`);
    }
    sub = made.sub;
    if (made.retried) renewed += "push service error on the first try, retried; ";
    done("subscribe", `${renewed}new subscription at ${new URL(sub.endpoint).host}`);
  } else done("subscribe", `existing subscription at ${new URL(sub.endpoint).host}`);

  const final = sub;
  await run("server", "Server přihlášení k oznámením nepřijal", () => postSubscription(final));
  done("server", "subscription saved on the server");
  return steps;
}

/** "Zapnout notifikace v telefonu" on the owner's item in "Čeká na tebe": the same flow as Settings. */
export async function enablePushFromClick(): Promise<void> {
  if (!pushSupported()) throw new Error("Tady oznámení zapnout nejdou. Otevři PersonalOS v telefonu (/m) a stiskni to tam.");
  await enablePush(null);
}

/** On every start of the app (and on opening Settings): a subscription the browser has but the server does not
 * (a failed POST, a subscription the server dropped) is registered again; with the permission granted and no
 * subscription at all (or one made with another key), one is made (no prompt is needed then). Silent; the result
 * is reported. */
export async function syncPush(cfg?: PushConfig): Promise<boolean> {
  if (!pushSupported() || Notification.permission !== "granted") return false;
  try {
    const c = cfg ?? (await pushApi.config());
    if (!c.enabled || !c.public_key) return false;
    const reg = await pushRegistration();
    let sub = await reg.pushManager.getSubscription();
    if (sub && !sameKey(sub, c.public_key)) {
      await sub.unsubscribe().catch(() => undefined);
      sub = null;
    }
    if (sub && c.this_device) return false;
    if (!sub) sub = (await subscribeTimed(reg, c.public_key)).sub;
    await postSubscription(sub);
    logPushStep("sync", true, `subscription at ${new URL(sub.endpoint).host} registered on app start`);
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
