import { useEffect, useState } from "react";
import { api } from "./api";
import { subscribe, useLiveStatus } from "./liveStream";

/** One thing that waits for the owner (GET /api/needs-me). */
export type NeedsItem = {
  kind: "approval" | "ask" | "review" | "mention";
  key: string;
  id: number;
  ref: string | null;
  title: string;
  detail: string;
  from_name: string | null;
  from_kind: "human" | "ai" | "agent" | "external" | null;
  at: string;
  link: string;
  /** An approval's action id (email_send…), shown in words. */
  action?: string;
  ask_kind?: string | null;
  blocking?: boolean;
  channel_id?: number;
  thread?: number;
};

export type NeedsMe = { count: number; counts: Record<NeedsItem["kind"], number>; items: NeedsItem[] };

// One shared copy: the Home inbox, the sidebar badge and the phone tab bar show the same count.
let state: NeedsMe | null = null;
const subs = new Set<(s: NeedsMe) => void>();
let inflight: Promise<void> | null = null;

export function refreshNeedsMe(): Promise<void> {
  if (!inflight)
    inflight = api<NeedsMe>("/api/needs-me")
      .then((s) => {
        state = s;
        subs.forEach((f) => f(s));
      }, () => undefined)
      .finally(() => {
        inflight = null;
      });
  return inflight;
}

/** Take an item off the list at once (after acting on it); the next refresh confirms it. */
export function dropNeedsItem(key: string) {
  if (!state) return;
  const items = state.items.filter((i) => i.key !== key);
  const counts = { approval: 0, ask: 0, review: 0, mention: 0 };
  items.forEach((i) => (counts[i.kind] += 1));
  state = { count: items.length, counts, items };
  subs.forEach((f) => f(state!));
}

// Pushed by the tab's live stream (liveStream.ts) whenever it changes.
let streamed = false;
function fromStream() {
  if (streamed) return;
  streamed = true;
  subscribe<NeedsMe>("needs", (s) => {
    state = s;
    subs.forEach((f) => f(s));
  });
}

export function useNeedsMe(): NeedsMe | null {
  const [s, setS] = useState<NeedsMe | null>(state);
  const live = useLiveStatus();
  useEffect(() => {
    // Polled only while the stream is down.
    if (live !== "down") return;
    const t = setInterval(() => document.visibilityState === "visible" && refreshNeedsMe(), 30000);
    return () => clearInterval(t);
  }, [live]);
  useEffect(() => {
    fromStream();
    subs.add(setS);
    // The stream's first snapshot normally comes within a second; HTTP only if it does not.
    const first = state ? undefined : window.setTimeout(() => !state && refreshNeedsMe(), 2500);
    if (state) setS(state);
    const on = () => refreshNeedsMe();
    window.addEventListener("pos:approvals", on);
    window.addEventListener("pos:needs-me", on);
    return () => {
      subs.delete(setS);
      window.clearTimeout(first);
      window.removeEventListener("pos:approvals", on);
      window.removeEventListener("pos:needs-me", on);
    };
  }, []);
  return s;
}
