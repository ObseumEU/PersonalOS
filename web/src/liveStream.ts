import { useEffect, useRef, useState } from "react";

/*
 * One live connection per tab: GET /api/stream (Server-Sent Events, backend pos.live). It replaces
 * the polling of health, the kill switch, subsystems and "Čeká na tebe", and tells pages when the
 * things they show changed ("changed" with domains: task, approval, run, ...).
 *
 * - Opened on the first subscriber, shared by everybody.
 * - Reconnects with backoff (2 s doubling to 60 s); while it is down, hooks fall back to slow
 *   polling of their old endpoints, so nothing goes stale.
 * - A tab hidden for HIDDEN_CLOSE_MS closes it (no idle connection in a forgotten tab, no battery
 *   drain on the phone); visible again → it reopens, gets a fresh snapshot, and pages reload.
 */

export type LiveStatus = "connecting" | "open" | "down";
export type Topic = "hello" | "freeze" | "needs" | "subsystems" | "presence" | "changed";

const HIDDEN_CLOSE_MS = 120_000;
const latest = new Map<Topic, unknown>();
const subs = new Map<Topic, Set<(data: unknown) => void>>();
const statusSubs = new Set<(s: LiveStatus) => void>();
let status: LiveStatus = "connecting";
let es: EventSource | null = null;
let backoff = 2000;
let retryTimer: number | undefined;
let hiddenTimer: number | undefined;
let started = false;

function setStatus(s: LiveStatus) {
  if (s === status) return;
  status = s;
  statusSubs.forEach((f) => f(s));
}

function emit(topic: Topic, data: unknown) {
  if (topic !== "changed") latest.set(topic, data);
  subs.get(topic)?.forEach((f) => f(data));
}

const TOPICS: Topic[] = ["hello", "freeze", "needs", "subsystems", "presence", "changed"];

function open() {
  if (es || typeof EventSource === "undefined") return;
  window.clearTimeout(retryTimer);
  const wasDown = status === "down";
  es = new EventSource("/api/stream");
  for (const topic of TOPICS)
    es.addEventListener(topic, ((e: MessageEvent) => {
      try {
        emit(topic, JSON.parse(e.data));
      } catch {
        /* a broken frame: the next snapshot fixes it */
      }
    }) as EventListener);
  es.onopen = () => {
    backoff = 2000;
    setStatus("open");
    if (wasDown) emit("changed", { domains: ["*"] }); // what pages missed while we were away
  };
  es.onerror = () => {
    // EventSource retries on its own, but without backoff and forever on a 401: do it ourselves.
    es?.close();
    es = null;
    setStatus("down");
    if (document.visibilityState === "hidden") return; // reopened when the tab is back
    retryTimer = window.setTimeout(open, backoff);
    backoff = Math.min(backoff * 2, 60_000);
  };
}

function close() {
  es?.close();
  es = null;
  window.clearTimeout(retryTimer);
}

function start() {
  if (started) return;
  started = true;
  open();
  document.addEventListener("visibilitychange", () => {
    window.clearTimeout(hiddenTimer);
    if (document.visibilityState === "hidden") {
      hiddenTimer = window.setTimeout(() => {
        close();
        setStatus("connecting");
      }, HIDDEN_CLOSE_MS);
    } else if (!es) {
      setStatus("down"); // so the reopen announces "changed: *"
      backoff = 2000;
      open();
    }
  });
}

/** Listen to one topic; the last value (if any) is delivered at once. */
export function subscribe<T>(topic: Topic, f: (data: T) => void): () => void {
  start();
  let set = subs.get(topic);
  if (!set) subs.set(topic, (set = new Set()));
  const fn = f as (d: unknown) => void;
  set.add(fn);
  if (latest.has(topic)) fn(latest.get(topic));
  return () => {
    set!.delete(fn);
  };
}

/** Follow the connection state (called at once with the current one). */
export function onStatus(f: (s: LiveStatus) => void): () => void {
  start();
  statusSubs.add(f);
  f(status);
  return () => {
    statusSubs.delete(f);
  };
}

export function liveStatus(): LiveStatus {
  return status;
}

export function useLiveStatus(): LiveStatus {
  const [s, setS] = useState(status);
  useEffect(() => {
    start();
    statusSubs.add(setS);
    setS(status);
    return () => {
      statusSubs.delete(setS);
    };
  }, []);
  return s;
}

/** Runs `poll` every `everyMs` only while the stream is down (and the tab visible). */
function useFallbackPoll(poll: (() => void) | undefined, everyMs: number) {
  const st = useLiveStatus();
  const ref = useRef(poll);
  ref.current = poll;
  useEffect(() => {
    if (st !== "down" || !ref.current) return;
    const h = window.setInterval(() => document.visibilityState === "visible" && ref.current?.(), everyMs);
    return () => window.clearInterval(h);
  }, [st, everyMs]);
}

/**
 * A topic's value, live. `fallback` loads the same thing over plain HTTP: once on mount when the
 * stream has not delivered yet, and every `everyMs` while the stream is down.
 */
export function useLive<T>(topic: Topic, fallback?: () => Promise<T>, everyMs = 60_000): T | undefined {
  const [v, setV] = useState<T | undefined>(latest.get(topic) as T | undefined);
  const load = fallback ? () => fallback().then(setV, () => undefined) : undefined;
  useEffect(() => subscribe<T>(topic, setV), [topic]);
  useEffect(() => {
    // The stream's first snapshot normally comes within a second; HTTP only if it does not.
    const h = window.setTimeout(() => !latest.has(topic) && load?.(), 2500);
    return () => window.clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic]);
  useFallbackPoll(load, everyMs);
  return v;
}

/**
 * Reload a page's data when the things it shows change: `domains` are audit-log entities
 * (task, approval, actor, schedule, ...) plus "run" (an agent started or finished working).
 * Changes are coalesced (one reload per `debounceMs`). While the stream is down it polls every
 * `fallbackMs`; `safetyMs` reloads anyway now and then (things that change without an audit entry).
 */
export function useLiveReload(
  domains: string[],
  load: () => void,
  { fallbackMs = 30_000, safetyMs = 300_000, debounceMs = 600 }: { fallbackMs?: number; safetyMs?: number; debounceMs?: number } = {},
) {
  const ref = useRef(load);
  ref.current = load;
  const key = domains.join(",");
  useEffect(() => {
    const want = new Set(key.split(","));
    let t: number | undefined;
    const off = subscribe<{ domains: string[] }>("changed", (d) => {
      if (!d.domains.some((x) => x === "*" || want.has(x))) return;
      window.clearTimeout(t);
      t = window.setTimeout(() => ref.current(), debounceMs);
    });
    const h = window.setInterval(() => document.visibilityState === "visible" && ref.current(), safetyMs);
    return () => {
      off();
      window.clearTimeout(t);
      window.clearInterval(h);
    };
  }, [key, debounceMs, safetyMs]);
  useFallbackPoll(() => ref.current(), fallbackMs);
}
