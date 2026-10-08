import { api } from "./api";
import type { FileItem } from "./filesApi";
import type { Task } from "./tasksApi";

export type ProjectStatus = "active" | "paused" | "done" | "archived";
export type Member = { actor_id: number; name: string; kind: string; role: "lead" | "member" | "helper" };
export type Links = {
  repos: string[];
  drive_folder: string | null;
  website: string | null;
  customer: string | null;
  customer_url: string | null;
};
export type Facts = { customer: string | null; contact: string | null; budget: string | null; stack: string | null };
export type ProjectInfo = {
  description: string;
  start_date: string | null;
  goal_progress: number | null;
  links: Links;
  facts: Facts;
  kb_workspace: string | null;
  keywords: string[];
  member_roles: Record<string, string>;
  kb_strict: boolean;
  updated_at: string | null;
};
export type LogEntry = {
  id: number;
  kind: "decision" | "milestone";
  date: string;
  text: string;
  why: string | null;
  who: string | null;
  source: string | null;
  created_at: string;
};
export type Person = Member & {
  project_role: string | null;
  open: number;
  now: { ref: string; title: string; status: Task["status"]; progress: number | null }[];
};
export type Project = {
  id: number;
  slug: string;
  name: string;
  goal: string | null;
  definition_of_done: string | null;
  status: ProjectStatus;
  visibility: string;
  lead_id: number | null;
  lead_name: string | null;
  lead_kind: string | null;
  labels: string[];
  due: string | null;
  channel_id: number | null;
  members: Member[];
  counts: { queued: number; working: number; review: number; done: number };
  total: number;
  info: ProjectInfo;
  summary?: string | null;
  updated_at: string;
  created_at: string;
  tasks?: Task[];
  decisions?: LogEntry[];
  people?: Person[];
  can_edit?: boolean;
};
export type Summary = { text: string; source: "llm" | "fallback"; fresh: boolean; at: string | null };
export type KbDoc = {
  id: string;
  name: string | null;
  kind: "repo" | "drive" | "mail";
  date: string | null;
  url: string | null;
  repo: string | null;
  path: string | null;
  channel: string | null;
  mime: string | null;
};
export type ProjectFiles = {
  files: FileItem[];
  available: boolean;
  url: string;
  workspace: string | null;
  drive_folder: string | null;
  repo_docs: KbDoc[];
  drive: KbDoc[];
  mail: KbDoc[];
};
export type ActivityItem = {
  at: string;
  kind: "commit" | "deploy" | "deploy_fail" | "pr" | "issue" | "release" | "mail" | "task_new" | "task_done" | "decision" | "milestone";
  title: string;
  who: string | null;
  url: string | null;
  ref: string | null;
  repo?: string;
  why?: string | null;
};
export type AskAnswer = {
  ok: boolean;
  answer?: string;
  error?: string;
  citations?: { n?: number; title?: string; url?: string; quote?: string; text?: string }[];
  insufficient_evidence?: boolean;
  url?: string;
  thread_id?: string | null;
};

export type RealityStatus = "live" | "unverified" | "test_only" | "mock" | "missing";
export type Capability = {
  id: number;
  key: string;
  name: string;
  status: RealityStatus;
  url: string | null;
  access: "public" | "password" | "internal";
  notes: string;
  verified_at: string | null;
  verify_method: string | null;
  last_check_at: string | null;
  last_check_ok: boolean | null;
  last_check_detail: string | null;
  expired: boolean;
  /** live (fresh), or test_only with a passing probe of the password-protected test instance */
  verified: boolean;
};
export type BlockedCheck = { id: number; at: string; agent: string | null; surface: string; reasons: string[]; excerpt: string; overridden: boolean };
export type RealityHold = { task_id: number; ref: string; title: string; status: string; assignee: string | null; capabilities: string[]; note: string; since: string };
export type Reality = {
  capabilities: Capability[];
  expiry_hours: number;
  blocked: BlockedCheck[];
  can_override: boolean;
  holds?: RealityHold[];
  /** the owner or the project lead: one-click release of a held task */
  can_release?: boolean;
};

const json = (method: string, body?: unknown): RequestInit => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

export const projectsApi = {
  list: (status?: string) => api<Project[]>(`/api/projects${status ? `?status=${status}` : ""}`),
  get: (ref: string) => api<Project>(`/api/projects/${ref}`),
  create: (body: Record<string, unknown>) => api<Project>("/api/projects", json("POST", body)),
  update: (ref: string, changes: Record<string, unknown>) => api<Project>(`/api/projects/${ref}`, json("PATCH", changes)),
  addMember: (ref: string, member: string | number, role = "member") => api<Project>(`/api/projects/${ref}/members`, json("POST", { member, role })),
  summary: (ref: string, generate: boolean) => api<Summary>(`/api/projects/${ref}/summary?generate=${generate}`),
  decide: (ref: string, body: { text: string; why?: string; who?: string; date?: string }) => api<LogEntry>(`/api/projects/${ref}/decisions`, json("POST", body)),
  dropDecision: (ref: string, id: number) => fetch(`/api/projects/${ref}/decisions/${id}`, { method: "DELETE", credentials: "same-origin" }),
  files: (ref: string) => api<ProjectFiles>(`/api/projects/${ref}/files`),
  linkFile: (ref: string, fileId: number) => api<{ ok: boolean }>(`/api/projects/${ref}/files`, json("POST", { file_id: fileId })),
  unlinkFile: (ref: string, fileId: number) => fetch(`/api/projects/${ref}/files/${fileId}`, { method: "DELETE", credentials: "same-origin" }),
  activity: (ref: string, days = 30) => api<{ days: number; items: ActivityItem[] }>(`/api/projects/${ref}/activity?days=${days}`),
  ask: (ref: string, question: string, effort: 1 | 2) => api<AskAnswer>(`/api/projects/${ref}/ask`, json("POST", { question, effort })),
  reality: (ref: string) => api<Reality>(`/api/projects/${ref}/reality`),
  probe: (ref: string) => api<{ capabilities: Capability[] }>(`/api/projects/${ref}/reality/probe`, json("POST")),
  release: (ref: string, taskId: number, reason = "") =>
    api<{ released: boolean; status: string | null }>(`/api/projects/${ref}/reality/holds/${taskId}/release`, json("POST", { reason })),
  override: (checkId: number, reason: string) => api<{ override_id: number }>(`/api/reality/checks/${checkId}/override`, json("POST", { reason })),
};

/** "doplnit" (a field the seed could not fill) reads as missing. */
export const missing = (v: string | null | undefined) => !v || v.trim().toLowerCase() === "doplnit";
