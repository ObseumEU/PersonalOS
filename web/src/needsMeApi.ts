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

export function useNeedsMe(): NeedsMe | null {
  const [s, setS] = useState<NeedsMe | null>(state);
  useEffect(() => {
    subs.add(setS);
    if (subs.size === 1) refreshNeedsMe();
    else if (state) setS(state);
    const t = setInterval(() => document.visibilityState === "visible" && refreshNeedsMe(), 20000);
    const on = () => refreshNeedsMe();
    window.addEventListener("pos:approvals", on);
    window.addEventListener("pos:needs-me", on);
    return () => {
      subs.delete(setS);
      clearInterval(t);
      window.removeEventListener("pos:approvals", on);
      window.removeEventListener("pos:needs-me", on);
    };
  }, []);
  return s;
}
