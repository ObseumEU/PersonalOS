import { api } from "./api";

export type AssigneeType = "human" | "ai" | "agent" | "external";
export type Status = "inbox" | "next" | "working" | "review" | "waiting" | "someday" | "done";
export type View = "inbox" | "today" | "upcoming" | "next" | "agents" | "waiting" | "review" | "someday" | "done";

export type Step = {
  title: string;
  assignee: string;
  reason?: string;
  estimate_min?: number | null;
};

export type Suggestion = {
  engine: "codex" | "rules";
  actionable: boolean;
  title: string;
  topic: string | null;
  priority: 1 | 2 | 3 | null;
  do_date: string | null;
  deadline: string | null;
  estimate_min: number | null;
  energy: "high" | "low" | null;
  assignee: string | null;
  two_minutes: boolean;
  definition_of_done: string | null;
  steps: Step[];
  rationale: string;
};

export type Task = {
  id: number;
  ref: string;
  parent_id: number | null;
  title: string;
  notes: string;
  // 1 when PersonalOS wrote the description from the task's fields (pos.task_descriptions).
  description_generated?: number;
  status: Status;
  priority: 1 | 2 | 3 | null;
  do_date: string | null;
  deadline: string | null;
  estimate_min: number | null;
  energy: "high" | "low" | null;
  topic: string | null;
  definition_of_done: string | null;
  visibility: "public" | "team" | "private";
  owner_id: number;
  assignee_type: AssigneeType | null;
  assignee_id: number | null;
  assignee_name: string | null;
  follow_up: string | null;
  progress: number | null;
  progress_note: string | null;
  returned_count: number;
  interventions: number;
  suggestion: Suggestion | null;
  source: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  steps_total?: number;
  steps_done?: number;
  steps?: Task[];
  parent?: { id: number; ref: string; title: string };
};

export type Actor = { id: number; kind: "human" | "ai" | "agent"; name: string; is_owner: number };
export type Version = { version: number; action: string; at: string; actor_name: string | null; run_id: number | null };
export type Counts = Record<Exclude<View, "done">, number>;

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const tasksApi = {
  list: (view: View, topic?: string) =>
    api<Task[]>(`/api/tasks?view=${view}${topic ? `&topic=${encodeURIComponent(topic)}` : ""}`),
  counts: () => api<Counts>("/api/tasks/counts"),
  topics: () => api<{ topic: string; open: number }[]>("/api/tasks/topics"),
  actors: () => api<Actor[]>("/api/actors"),
  get: (ref: string) => api<Task>(`/api/tasks/${ref}`),
  capture: (text: string) => post<Task>("/api/tasks/capture", { text }),
  update: (ref: string, changes: Record<string, unknown>) =>
    api<Task>(`/api/tasks/${ref}`, { method: "PATCH", body: JSON.stringify(changes) }),
  addStep: (ref: string, title: string, assignee?: string) =>
    post<Task>(`/api/tasks/${ref}/steps`, { title, ...(assignee ? { assignee } : {}) }),
  complete: (ref: string) => post<Task>(`/api/tasks/${ref}/complete`),
  review: (ref: string, accept: boolean, comment?: string) => post<Task>(`/api/tasks/${ref}/review`, { accept, comment }),
  intervene: (ref: string, note: string) => post<Task>(`/api/tasks/${ref}/intervene`, { note }),
  archive: (ref: string) => post<Task>(`/api/tasks/${ref}/archive`),
  suggest: (ref: string) => post<Suggestion>(`/api/tasks/${ref}/suggest`),
  clarify: (ref: string, action: string, fields?: Record<string, unknown>) =>
    post<Task>(`/api/tasks/${ref}/clarify`, { action, fields }),
  history: (ref: string) => api<Version[]>(`/api/tasks/${ref}/history`),
  restore: (ref: string, version: number) => post<Task>(`/api/tasks/${ref}/restore`, { version }),
};

export const PRIORITY_LABEL: Record<number, string> = { 1: "Must", 2: "Should", 3: "Could" };

export function dueLabel(t: Pick<Task, "do_date" | "deadline" | "follow_up" | "status">): { text: string; urgent: boolean } {
  const today = new Date().toISOString().slice(0, 10);
  const tomorrow = new Date(Date.now() + 864e5).toISOString().slice(0, 10);
  const fmt = (d: string) =>
    d === today ? "today" : d === tomorrow ? "tomorrow" : new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  if (t.deadline && t.deadline <= today && t.status !== "done")
    return { text: t.deadline < today ? "overdue" : "due today", urgent: true };
  const d = t.do_date ?? t.deadline ?? (t.status === "waiting" ? t.follow_up : null);
  if (!d) return { text: "", urgent: false };
  return { text: fmt(d), urgent: d <= today };
}

export const NO_DESCRIPTION = "Bez popisu";

/** A short plain-text preview of a task's description (markdown marks and wrappers stripped). */
export function descriptionPreview(notes: string | null | undefined, max = 180): string {
  const text = (notes ?? "")
    .replace(/<\/?external[^>]*>/g, " ")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/^_Generated from the task's fields.*$/m, "")
    .replace(/\*\*|__|`/g, "")
    .replace(/^\s*(#{1,6}|>|[-+*]|\d+\.)\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}
