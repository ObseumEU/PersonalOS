import { api } from "./api";
import type { Network } from "./components/agents/AgentNetwork";
import type { Task } from "./tasksApi";

export type AgentStatus = "online" | "working" | "idle" | "approval" | "paused" | "archived";

export type Agent = {
  id: number;
  name: string;
  kind: "human" | "ai" | "agent";
  is_owner: boolean;
  runtime: string;
  a2a_url: string | null;
  purpose: string | null;
  lifetime: "one_shot" | "long_lived" | null;
  system: boolean;
  permissions: string[];
  budget_class: string | null;
  status: AgentStatus;
  last_seen_at: string | null;
  paused: boolean;
  archived: boolean;
  created_by: number | null;
  created_by_name: string | null;
  created_at: string;
  expires_at: string | null;
  role: string | null;
  team: string | null;
  reports_to: number | null;
  reports_to_name: string | null;
  engine: "claude" | "codex" | "auto" | null;
  engine_effective: "claude" | "codex" | "auto";
  model: string | null;
  engine_view: EngineView | null;
  tokens_24h: number;
  tokens_7d: number;
  daily_cap: number | null;
  queued: number;
  working: number;
  review: number;
  done_today: number;
  approvals_waiting: number;
  current: { id: number; ref: string; title: string } | null;
};

/** Which engine and model an agent runs on (read-only, from /api/agents). */
export type EngineRun = { engine: "claude" | "codex"; model: string | null; fallback: boolean; label: string };
export type EngineView = {
  setting: "claude" | "codex" | "auto";
  primary: "claude" | "codex";
  now: (Partial<EngineRun> & { label: string; paused_until?: string | null }) | { engine: null; label: string };
  last_run: (EngineRun & { at: string; running: boolean }) | null;
};

export type TraceEntry = {
  id: number;
  at: string;
  action: string;
  via: string;
  entity: string | null;
  entity_id: number | null;
  actor_name: string | null;
  detail: Record<string, unknown>;
};

export type Run = {
  id: number;
  kind: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  task_id: number | null;
  detail: string;
};

export type AgentDetail = Agent & {
  instructions: string | null;
  queue: Task[];
  runs: Run[];
  trace: TraceEntry[];
  memory: { id: number; body: string; visibility: string; created_at: string }[];
  week: { done: number; returned: number; interventions: number };
  api_key?: string;
};

export type BoardCard = { ref?: string; id?: number; approval_id?: number; title: string; status: string; progress?: number | null };
export type BoardRow = {
  actor: { id: number; name: string; kind: Agent["kind"]; is_owner: boolean; status: AgentStatus };
  queued: BoardCard[];
  working: BoardCard[];
  needs_you: BoardCard[];
  done_today: BoardCard[];
};

export type Approval = {
  id: number;
  task_id: number | null;
  task_ref: string | null;
  requested_by: number;
  requested_by_name: string;
  requested_by_kind: Agent["kind"];
  action: string;
  details: Record<string, unknown>;
  status: "pending" | "approved" | "rejected";
  created_at: string;
  decided_at: string | null;
  comment: string | null;
  result?: { status: string; owner_task?: string; error?: string } | null;
};

export type Engines = {
  default: string;
  codex: { report: { level?: string; windows?: Record<string, { used_pct?: number; resets_at?: string }> } | null; can_run: boolean };
  claude: {
    window_5h: { tokens: number; cost_usd: number; runs: number };
    window_7d: { tokens: number; cost_usd: number; runs: number };
    paused_until: string | null;
    last_limit: { window: string | null; resets_at: string | null; state: string | null; reason: string | null } | null;
  };
};

/** A member's place in the company structure (GET /api/org). */
export type OrgMember = {
  id: number;
  name: string;
  kind: Agent["kind"];
  is_owner: boolean;
  role: string | null;
  team: string | null;
  reports_to: number | null;
  reports_to_name: string | null;
  status: AgentStatus;
  level: number;
};
export type Org = { members: OrgMember[]; roles: string[]; project_manager: number | null };

export type FreezeState = { frozen: boolean; reason?: string; updated_at?: string };

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const agentsApi = {
  list: () => api<{ agents: Agent[]; permissions: Record<string, string> }>("/api/agents"),
  get: (id: number) => api<AgentDetail>(`/api/agents/${id}`),
  create: (body: Record<string, unknown>) =>
    post<{ created: boolean; agent?: AgentDetail; api_key?: string; reason?: string; decision?: string; limit?: string }>(
      "/api/agents",
      body,
    ),
  setPermissions: (id: number, permissions: string[]) =>
    api<AgentDetail>(`/api/agents/${id}/permissions`, { method: "PUT", body: JSON.stringify({ permissions }) }),
  action: (id: number, action: "pause" | "resume" | "stop" | "archive" | "restore") => post<AgentDetail>(`/api/agents/${id}/${action}`),
  message: (id: number, body: string, task_id?: string, priority: "fyi" | "change_plan" | "stop" = "fyi") =>
    post(`/api/agents/${id}/message`, { body, task_id, priority }),
  board: () => api<BoardRow[]>("/api/board"),
  approvals: (status: "pending" | "all" = "pending") => api<Approval[]>(`/api/approvals?status=${status}`),
  decide: (id: number, approve: boolean, comment?: string) => post<Approval>(`/api/approvals/${id}/decide`, { approve, comment }),
  setEngine: (id: number, engine: string | null, model: string | null) =>
    api<AgentDetail>(`/api/agents/${id}/engine`, { method: "PUT", body: JSON.stringify({ engine, model }) }),
  engines: () => api<Engines>("/api/engines"),
  org: () => api<Org>("/api/org"),
  setOrg: (id: number, body: { role?: string | null; team?: string | null; reports_to?: number | null }) =>
    api<OrgMember>(`/api/agents/${id}/org`, { method: "PUT", body: JSON.stringify(body) }),
  network: (window: string) => api<Network>(`/api/network?window=${window}`),
  freezeState: () => api<FreezeState>("/api/system/freeze"),
  freeze: (reason: string) => post<FreezeState>("/api/system/freeze", { reason }),
  unfreeze: () => post<FreezeState>("/api/system/unfreeze"),
};
