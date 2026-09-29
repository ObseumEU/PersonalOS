import { useEffect, useState } from "react";
import type { ChatMessage, Presence, StreamEvent } from "../chatApi";

/*
 * One live connection for the whole app (/api/chat/stream, Server-Sent Events): the list and
 * the open conversation both listen. Closed while the app is in the background (no battery
 * drain; push notifications cover that time) and opened again when it comes back.
 */

type Listener = (ev: StreamEvent) => void;
const listeners = new Set<Listener>();
const presenceSubs = new Set<(p: Presence) => void>();
const statusSubs = new Set<(on: boolean) => void>();
let presence: Presence = { working: [], typing: {} };
let es: EventSource | null = null;
let connected = false;

function setConnected(on: boolean) {
  connected = on;
  statusSubs.forEach((f) => f(on));
}

function open() {
  if (es) return;
  es = new EventSource("/api/chat/stream");
  const onMsg = (e: MessageEvent) => {
    const ev = JSON.parse(e.data) as StreamEvent;
    listeners.forEach((f) => f(ev));
  };
  ["message", "edit", "archive", "reaction", "channel"].forEach((x) => es!.addEventListener(x, onMsg as EventListener));
  es.addEventListener("presence", ((e: MessageEvent) => {
    presence = JSON.parse(e.data);
    presenceSubs.forEach((f) => f(presence));
  }) as EventListener);
  es.onopen = () => setConnected(true);
  es.onerror = () => setConnected(false);
}

function close() {
  es?.close();
  es = null;
  setConnected(false);
}

let started = false;
function start() {
  if (started) return;
  started = true;
  open();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      open();
      window.dispatchEvent(new Event("pos:resume")); // lists reload what they missed
    } else close();
  });
}

export function onChatEvent(f: Listener): () => void {
  start();
  listeners.add(f);
  return () => listeners.delete(f);
}

export function usePresence(): Presence {
  const [p, setP] = useState(presence);
  useEffect(() => {
    start();
    presenceSubs.add(setP);
    return () => {
      presenceSubs.delete(setP);
    };
  }, []);
  return p;
}

export function useLive(): boolean {
  const [on, setOn] = useState(connected);
  useEffect(() => {
    start();
    statusSubs.add(setOn);
    return () => {
      statusSubs.delete(setOn);
    };
  }, []);
  return on;
}

export const isMessageEvent = (ev: StreamEvent): ev is StreamEvent & { message: ChatMessage } => "message" in ev;
