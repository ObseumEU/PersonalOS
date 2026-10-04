import { useEffect, useState } from "react";
import { api } from "./api";

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
  /** A decision card (ask_owner with options): a button per option, the recommended one, and when it
   * applies by itself without an answer (null: only his click). */
  options?: string[];
  recommendation?: string | null;
  default_at?: string | null;
};

/** The owner pressed an option on a decision card. */
export function chooseOption(ref: string, option: string) {
  return api<{ ref: string; decided: string }>(`/api/needs-me/asks/${encodeURIComponent(ref)}/choose`, {
    method: "POST",
    body: JSON.stringify({ option }),
  });
}

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

// Pushed by the tab's live stream (liveStream.ts) whenever it changes; polled only while it is
// down. Imported on demand, so the installed app's first load (/m budget) does not carry it.
let streamed = false;
let streamDown = false;
function fromStream() {
  if (streamed) return;
  streamed = true;
  void import("./liveStream").then((live) => {
    live.subscribe<NeedsMe>("needs", (s) => {
      state = s;
      subs.forEach((f) => f(s));
    });
    live.onStatus((st) => (streamDown = st === "down"));
  });
}

export function useNeedsMe(): NeedsMe | null {
  const [s, setS] = useState<NeedsMe | null>(state);
  useEffect(() => {
    fromStream();
    const poll = setInterval(() => streamDown && document.visibilityState === "visible" && refreshNeedsMe(), 30000);
    subs.add(setS);
    // The stream's first snapshot normally comes within a second; HTTP only if it does not.
    const first = state ? undefined : window.setTimeout(() => !state && refreshNeedsMe(), 2500);
    if (state) setS(state);
    const on = () => refreshNeedsMe();
    window.addEventListener("pos:approvals", on);
    window.addEventListener("pos:needs-me", on);
    return () => {
      subs.delete(setS);
      clearInterval(poll);
      window.clearTimeout(first);
      window.removeEventListener("pos:approvals", on);
      window.removeEventListener("pos:needs-me", on);
    };
  }, []);
  return s;
}
