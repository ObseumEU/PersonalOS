import { useEffect, useState } from "react";
import { api } from "./api";
import type { ApprovalView } from "./components/ApprovalBody";

/** One thing that waits for the owner (GET /api/needs-me). */
export type NeedsItem = {
  kind: "handoff" | "setup" | "approval" | "access" | "publish" | "draft" | "ask" | "review" | "mention";
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
  /** An approval as the owner reads it. */
  view?: ApprovalView;
  /** Where he acts on it outside PersonalOS (Gmail drafts…). */
  links?: { label: string; href: string }[];
  ask_kind?: string | null;
  blocking?: boolean;
  channel_id?: number;
  thread?: number;
  /** A decision card (ask_owner with options): a button per option, the recommended one, and when it
   * applies by itself without an answer (null: only his click). */
  options?: string[];
  recommendation?: string | null;
  default_at?: string | null;
  /** access: the endpoint that grants or denies the request (owner only). */
  decide_url?: string;
  task_ref?: string | null;
  /** publish: an approved LinkedIn post not published yet. */
  publish_url?: string;
  connect_url?: string;
  connected?: boolean;
  text?: string;
  error?: string | null;
  /** handoff: an agent's live browser waits for one step of his (pos.handoff); m_link opens it in /m. */
  m_link?: string;
  status?: string;
  expired?: boolean;
  /** handoff: no live page yet (the agent prepares it when he opens it). */
  parked?: boolean;
  /** setup: "Skrýt" (the item does not come back). */
  hide_url?: string;
  expires_at?: string;
  /** draft: one waiting Gmail draft; an ask of a draft campaign lists its drafts. */
  draft?: WaitingDraft;
  drafts?: WaitingDraft[];
};

export type WaitingDraft = {
  id: number;
  to: string | null;
  subject: string;
  why: string;
  link: string | null;
  agent: string | null;
  mark_url: string;
};

/** The one-click owner actions on access, publish and draft items (endpoints come with the item). */
export const ownerActions = {
  decideAccess: (url: string, grant: boolean) =>
    api(url, {
      method: "POST",
      body: JSON.stringify({ decision: grant ? "grant" : "deny", note: grant ? "Schváleno majitelem." : "Zamítnuto majitelem." }),
    }),
  publish: (url: string) => api<{ status: string; url?: string }>(url, { method: "POST", body: "{}" }),
  /** "Připojit LinkedIn": an agent prepares everything in its browser; he only logs in and confirms. */
  connectLinkedIn: () =>
    api<{ task_ref: string; agent: string; existing: boolean }>("/api/integrations/linkedin/agent-connect", { method: "POST", body: "{}" }),
  /** An expired handoff: Pokračovat (the agent prepares the page again) or Zahodit. */
  handoffResume: (id: number) => api(`/api/handoffs/${id}/resume`, { method: "POST", body: "{}" }),
  handoffDismiss: (id: number) => api(`/api/handoffs/${id}/cancel`, { method: "POST", body: "{}" }),
  hide: (url: string) => api(url, { method: "POST", body: "{}" }),
  markDraft: (url: string, state: "sent" | "discarded") => api(url, { method: "POST", body: JSON.stringify({ state }) }),
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
// The last load failed (and nothing came since): the screens show it with "Zkusit znovu", not "načítám…" forever.
let failed: string | null = null;
const failSubs = new Set<(e: string | null) => void>();
function setFailed(e: string | null) {
  if (e === failed) return;
  failed = e;
  failSubs.forEach((f) => f(e));
}

export function refreshNeedsMe(): Promise<void> {
  if (!inflight)
    inflight = api<NeedsMe>("/api/needs-me")
      .then((s) => {
        state = s;
        setFailed(null);
        subs.forEach((f) => f(s));
      }, (e) => setFailed(e instanceof Error ? e.message : String(e)))
      .finally(() => {
        inflight = null;
      });
  return inflight;
}

/** Take an item off the list at once (after acting on it); the next refresh confirms it. */
export function dropNeedsItem(key: string) {
  if (!state) return;
  const items = state.items.filter((i) => i.key !== key);
  const counts = { handoff: 0, setup: 0, approval: 0, access: 0, publish: 0, draft: 0, ask: 0, review: 0, mention: 0 };
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
      setFailed(null);
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

/** Why the list could not load (null: it loaded, or is loading). */
export function useNeedsMeError(): string | null {
  const [e, setE] = useState<string | null>(failed);
  useEffect(() => {
    failSubs.add(setE);
    setE(failed);
    return () => {
      failSubs.delete(setE);
    };
  }, []);
  return e;
}
