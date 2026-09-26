import { api } from "./api";

/** A number with last week's value (flow: same part of last week; snapshots: the last report). */
export type Kpi = { value: number | null; prev: number | null; delta: number | null };

export type Goal = {
  id: number;
  title: string;
  why: string;
  target: string;
  owner_name: string | null;
  due: string | null;
  status: "active" | "paused" | "done" | "dropped";
  progress: number | null;
  progress_effective: number;
  parent_id: number | null;
  parent_title: string | null;
  links: { kind: string; ref: string }[];
  tasks: { total: number; done: number; open: number };
};

export type PacketGoal = {
  id: number;
  title: string;
  target: string;
  owner: string | null;
  due: string | null;
  status: string;
  progress: number;
  progress_prev: number | null;
  delta: number | null;
  parent_id: number | null;
  tasks_done: number;
  tasks_total: number;
};

export type TaskLine = { ref: string; title: string; assignee?: string | null; status?: string; deadline?: string | null };

export type Packet = {
  week: string;
  period: { start: string; end: string; until: string; partial: boolean; compared_with: string };
  generated_at: string;
  kpis: Record<string, Kpi>;
  tasks: {
    done: number;
    new: number;
    in_progress: number;
    review: number;
    waiting: number;
    overdue: number;
    done_by_day: { date: string; day: string; done: number; prev: number }[];
    by_project: { name: string; done: number; new: number; open: number }[];
    by_assignee: { name: string; kind: string; done: number; open: number; waiting: number }[];
    highlights: (TaskLine & { priority: number | null; project: string | null })[];
    overdue_list: TaskLine[];
    waiting_list: (TaskLine & { since: string; why: string })[];
  };
  agents: {
    runs: number;
    success_rate: number | null;
    cost_usd: number;
    tokens: number;
    accepted: number;
    cost_per_accepted: number | null;
    handbacks: number;
    returned: number;
    per_agent: {
      name: string;
      runs: number;
      ok: number;
      errors: number;
      success_rate: number | null;
      cost_usd: number;
      tokens: number;
      accepted: number;
      cost_per_accepted: number | null;
      handbacks: number;
    }[];
  };
  dev: {
    available: boolean;
    note: string;
    commits: number;
    merges: number;
    repos: { repo: string; commits: number; merges: number; prev: number; authors: string[] }[];
    deploys: { ok?: number; reverted?: number; rejected?: number; error?: number };
  };
  communication: {
    available: boolean;
    note: string;
    items?: number;
    by_origin?: Record<string, number>;
    top_channels?: { origin: string; channel: string; items: number }[];
  };
  incidents: {
    failed_deploys: { status: string; stage: string; author: string; at: string; sha: string }[];
    failed_runs: number;
    freezes: number;
    engine_limit_hits: number;
  };
  goals: PacketGoal[];
  last_meeting: { week: string; status: string; decisions: string[]; tasks: TaskLine[]; tasks_done: number } | null;
};

export type ReportStatus = "draft" | "meeting" | "published" | "closed" | "no_reply";

export type ReportListItem = {
  week: string;
  status: ReportStatus;
  period_start: string;
  period_end: string;
  headline: string;
  summary: string;
  kpis: Record<string, Kpi>;
  url: string;
};

export type Report = {
  week: string;
  status: ReportStatus;
  period_start: string;
  period_end: string;
  headline: string;
  narrative: string;
  decisions: string[];
  questions: string[];
  meeting_notes: string;
  meeting_summary: string;
  published_at: string | null;
  closed_at: string | null;
  asked_at: string | null;
  deadline_at: string | null;
  url: string;
  packet: Packet;
  tasks_created: TaskLine[];
  goals_changed: PacketGoal[];
  transcript: { id: number; author: string; kind: string; owner: boolean; at: string; body: string }[];
};

export const reportsApi = {
  list: () =>
    api<{ reports: ReportListItem[]; current_week: string; schedule: string | null }>("/api/reports"),
  get: (week: string) => api<Report>(`/api/reports/${encodeURIComponent(week)}`),
  build: (week: string) => api<{ week: string; summary: string }>(`/api/reports/${encodeURIComponent(week)}/build`, { method: "POST" }),
  goals: (status = "active") => api<Goal[]>(`/api/goals?status=${status}`),
};
