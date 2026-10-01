import { api } from "./api";
import type { RefKind } from "./refs";

/** The owner's report on a hand-in (backend pos.owner_report). */
export type Decision = {
  id: string;
  question: string;
  options: string[];
  recommendation: string;
  why: string;
  context?: string;
  decided: { choice: string; note: string; by: string | null; at: string } | null;
};

export type Source = { title: string; quote: string; link: string; ref?: string };
export type Related = { kind: RefKind; id: string | number; title: string; text?: string; link: string; status?: string; at?: string };

export type OwnerReport =
  | { available: false; task: string; can_build?: boolean }
  | {
      available: true;
      task: string;
      title: string;
      status: string;
      assignee: string | null;
      source: "agent" | "builder";
      author: string | null;
      stale: boolean;
      built_at: string;
      takeaway: string;
      takeaway_source: "agent" | "llm" | "fallback";
      decisions: Decision[];
      decisions_open: number;
      decided_earlier: { id: string; question: string; choice: string; note: string; by: string | null; at: string }[];
      next: string;
      details: {
        summary: string[];
        content: string;
        content_title: string | null;
        changes: string[];
        verification: string[];
        sources: Source[];
        related: Related[];
        unresolved: { kind: RefKind; id: string; raw: string | null; why: string }[];
        original: string;
        dropped: string[];
      };
      report_url: string;
      resumed?: boolean;
    };

export type RefPreview = {
  kind: RefKind;
  id: string;
  ok: boolean;
  error?: string;
  title?: string;
  text?: string;
  quote?: string;
  link?: string | null;
  at?: string | null;
  author?: string;
  channel?: string;
  status?: string;
  assignee?: string | null;
  result?: string;
  context?: { id: number; author: string; text: string; at: string }[];
  download?: string;
  truncated?: boolean;
};

export const reportApi = {
  get: (ref: string, generate = true) => api<OwnerReport>(`/api/tasks/${ref}/report?generate=${generate}`),
  rebuild: (ref: string) => api<OwnerReport>(`/api/tasks/${ref}/report/rebuild`, { method: "POST" }),
  decide: (ref: string, decision: string, choice: string, note = "") =>
    api<OwnerReport>(`/api/tasks/${ref}/report/decisions`, { method: "POST", body: JSON.stringify({ decision, choice, note }) }),
  ref: (kind: RefKind, id: string) => api<RefPreview>(`/api/refs/${kind}:${encodeURIComponent(id)}`),
};

const cache = new Map<string, Promise<RefPreview>>();
/** One request per reference per page load (chips show titles, the drawer the content). */
export function loadRef(kind: RefKind, id: string): Promise<RefPreview> {
  const k = `${kind}:${id}`;
  let p = cache.get(k);
  if (!p) {
    p = reportApi.ref(kind, id).catch((e) => ({ kind, id, ok: false, error: e instanceof Error ? e.message : String(e) }));
    cache.set(k, p);
  }
  return p;
}

/** Where the full report opens: /m/report/T-1 in the installed app, /report/T-1 in the web app. */
export function reportHref(ref: string): string {
  return window.location.pathname.startsWith("/m") ? `/m/report/${ref}` : `/report/${ref}`;
}
