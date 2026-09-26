import { api } from "./api";
import { markdownSnippet } from "./markdownText";

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
  reviewer_id?: number | null;
  reviewer_effective_id?: number;
  reviewer_name?: string;
  can_review?: boolean;
  source: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  steps_total?: number;
  steps_done?: number;
  steps?: Task[];
  parent?: { id: number; ref: string; title: string };
};

export type Comment = {
  id: number;
  task_id: number;
  kind: "comment" | "return" | "review" | "handoff" | "progress" | "system";
  body: string;
  author_id: number | null;
  author_name: string | null;
  author_kind: string | null;
  created_at: string;
};

/** Whose tasks: assigned to me, to me and everyone below me, or everyone's. */
export type Scope = "mine" | "team" | "all";

export type Actor = { id: number; kind: "human" | "ai" | "agent"; name: string; is_owner: number };
export type Version = { version: number; action: string; at: string; actor_name: string | null; run_id: number | null };
export type Counts = Record<Exclude<View, "done">, number>;

/** Why a member cannot take a task now. Soft reasons (pause, kill switch, budget) pass on the owner's say. */
export type Blocker = { code: string; text: string; soft: boolean };

/** Someone a task can go to, for the agent picker (pos.reassign.candidates). */
export type Candidate = {
  id: number;
  name: string;
  kind: "human" | "ai" | "agent";
  is_owner: boolean;
  role: string | null;
  team: string | null;
  engine: string | null;
  model: string | null;
  engine_label: string | null;
  current: boolean;
  available: boolean;
  blocked: Blocker[];
  worker_online: boolean | null;
};

export type ReassignResult = {
  task: Task;
  from: string | null;
  to: string;
  cancelled_runs: number[];
  message_id: number | null;
  woke_workers: number;
  waiting_for: Blocker[];
};

export type LiveRun = {
  id: number;
  status: string;
  engine: string | null;
  model: string | null;
  label: string | null;
  actor_id: number;
  actor_name: string;
  started_at: string;
  ended_at: string | null;
  heartbeat_at: string | null;
  detail: string | null;
};

/** The task's live state (pos.reassign.live). */
export type TaskLive = {
  ref: string;
  status: Status;
  state: string;
  progress: number | null;
  progress_note: string | null;
  assignee: {
    id: number;
    name: string;
    kind: string;
    role: string | null;
    engine?: string | null;
    model?: string | null;
    engine_label?: string | null;
    worker_online?: boolean;
    worker_waiting?: boolean;
  } | null;
  run: LiveRun | null;
  runs: LiveRun[];
  blocked: Blocker[];
  notes: { at: string; by: string | null; note: string; progress: number | null; action: string }[];
  at: string;
};

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const tasksApi = {
  list: (view: View | "to_review", topic?: string, scope: Scope = "all") =>
    api<Task[]>(`/api/tasks?view=${view}&scope=${scope}${topic ? `&topic=${encodeURIComponent(topic)}` : ""}`),
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
  comments: (ref: string) => api<Comment[]>(`/api/tasks/${ref}/comments`),
  comment: (ref: string, body: string) => post<Comment>(`/api/tasks/${ref}/comments`, { body }),
  history: (ref: string) => api<Version[]>(`/api/tasks/${ref}/history`),
  restore: (ref: string, version: number) => post<Task>(`/api/tasks/${ref}/restore`, { version }),
  reassignOptions: (ref: string) => api<Candidate[]>(`/api/tasks/${ref}/reassign/options`),
  /** Hand the task to someone: the old run stops, the new agent is told and starts at once. */
  reassign: (ref: string, to: number | string, note?: string, force = false) =>
    post<ReassignResult>(`/api/tasks/${ref}/reassign`, { to, note: note || null, force }),
  live: (ref: string) => api<TaskLive>(`/api/tasks/${ref}/live`),
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
  return markdownSnippet((notes ?? "").replace(/^_Generated from the task's fields.*$/m, ""), max);
}
