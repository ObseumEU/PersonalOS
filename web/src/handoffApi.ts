import { api } from "./api";

/** An agent's live browser waiting for the owner (pos.handoff). */
export type Handoff = {
  id: number;
  status: "waiting" | "active" | "done" | "cancelled" | "expired";
  title: string;
  reason: string;
  url: string | null;
  agent: { id: number; name: string };
  task_ref: string | null;
  created_at: string;
  expires_at: string;
  opened_at: string | null;
  finished_at: string | null;
  finished_by: string | null;
  closed: boolean;
  frame_version: number;
  page: { url?: string; title?: string; w?: number; h?: number };
};

/** One input event for the agent's page; x and y are 0..1 of the page's viewport. */
export type HandoffEvent =
  | { t: "click"; x: number; y: number; button?: "left" | "right" | "middle"; n?: number }
  | { t: "move"; x: number; y: number }
  | { t: "wheel"; dx: number; dy: number; x?: number; y?: number }
  | { t: "key"; key: string }
  | { t: "text"; text: string };

const base = (id: number) => `/api/handoffs/${id}`;

export const handoffApi = {
  get: (id: number) => api<Handoff>(base(id)),
  open: (id: number, app: "web" | "m") => api<Handoff>(`${base(id)}/open`, { method: "POST", body: JSON.stringify({ app }) }),
  input: (id: number, events: HandoffEvent[]) =>
    api<{ accepted: number }>(`${base(id)}/input`, { method: "POST", body: JSON.stringify({ events }) }),
  done: (id: number, keepLogin: boolean) =>
    api<Handoff>(`${base(id)}/done`, { method: "POST", body: JSON.stringify({ keep_login: keepLogin }) }),
  cancel: (id: number) => api<Handoff>(`${base(id)}/cancel`, { method: "POST", body: "{}" }),
  resume: (id: number) => api<Handoff>(`${base(id)}/resume`, { method: "POST", body: "{}" }),
};

export const isOpen = (s: Handoff["status"]) => s === "waiting" || s === "active";

/** Playwright's name for a key the owner pressed (with Control/Alt/Meta/Shift as needed), or null for a
 * printable character that goes in as text. */
export function keyName(e: { key: string; ctrlKey: boolean; altKey: boolean; metaKey: boolean; shiftKey: boolean }): string | null {
  const named: Record<string, string> = { " ": "Space", Esc: "Escape", Del: "Delete", Up: "ArrowUp", Down: "ArrowDown", Left: "ArrowLeft", Right: "ArrowRight" };
  let k = named[e.key] ?? e.key;
  if (["Control", "Shift", "Alt", "Meta", "CapsLock", "Dead", "Unidentified", "Process"].includes(k)) return "";
  const mod = e.ctrlKey || e.altKey || e.metaKey;
  if (k.length === 1 && !mod) return null; // typed text
  if (k.length === 1) k = k.toLowerCase();
  const mods = [e.ctrlKey && "Control", e.altKey && "Alt", e.metaKey && "Meta", e.shiftKey && k.length > 1 && "Shift"].filter(Boolean);
  return [...mods, k].join("+");
}

/** Where a pointer is on the picture, as 0..1 of the page (the picture shows the whole viewport). */
export function pointAt(clientX: number, clientY: number, r: { left: number; top: number; width: number; height: number }) {
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  return { x: clamp((clientX - r.left) / (r.width || 1)), y: clamp((clientY - r.top) / (r.height || 1)) };
}

/** Minutes left until the handoff times out (0 when past). */
export function minutesLeft(expiresAt: string, now = Date.now()): number {
  return Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 60000));
}
