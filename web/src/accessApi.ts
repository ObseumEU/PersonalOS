import { api } from "./api";

/** Access and budgets (pos.access, /api/access). */
export type Grant = {
  id: number;
  agent_id: number;
  capability: string;
  kind: "permission" | "tool" | "outbound" | "scope" | "owner_only";
  granted_by: number | null;
  granted_by_name: string | null;
  source: string;
  reason: string;
  request_id: number | null;
  created_at: string;
  expires_at: string | null;
  ended_at: string | null;
  end_kind: string | null;
  end_reason: string | null;
  active: boolean;
};

export type BudgetLine = {
  label: string;
  limit: number | null;
  used: number | null;
  expires_at: string | null;
  reason: string | null;
  set_by: string | null;
  id: number | null;
};

export type AccessRequest = {
  id: number;
  agent_id: number;
  agent_name: string;
  trigger: "request" | "limit_hit" | "spike";
  what: "capability" | "budget" | "review";
  capability: string | null;
  metric: string | null;
  amount: number | null;
  hours: number | null;
  why: string;
  task_ref: string | null;
  blocking: number;
  needs_owner: boolean;
  status: "pending" | "granted" | "denied" | "escalated";
  decided_by_name: string | null;
  decision_note: string | null;
  created_at: string;
};

export type AccessEvent = {
  id: number;
  at: string;
  action: string;
  actor_name: string | null;
  entity_id: number | null;
  detail: Record<string, unknown>;
};

export type AgentAccess = {
  managed: boolean;
  is_manager: boolean;
  grants: Grant[];
  ended: Grant[];
  budgets: Record<string, BudgetLine>;
  requests: AccessRequest[];
  recent_requests: AccessRequest[];
  history: AccessEvent[];
};

export type CompanyAccess = {
  budgets: Record<string, BudgetLine>;
  settings: Record<string, number>;
  frozen: boolean;
  requests: AccessRequest[];
  history: AccessEvent[];
  metrics: Record<string, string>;
  manager_id: number | null;
  litellm: boolean;
};

const send = <T,>(method: string, path: string, body: unknown) => api<T>(path, { method, body: JSON.stringify(body) });

export const accessApi = {
  agent: (id: number) => api<AgentAccess>(`/api/access/agents/${id}`),
  company: () => api<CompanyAccess>("/api/access"),
  grant: (agent_id: number, capability: string, reason: string, hours: number | null) =>
    send("POST", "/api/access/grants", { agent_id, capability, reason, hours }),
  revoke: (grantId: number, reason: string) => send("POST", `/api/access/grants/${grantId}/revoke`, { reason }),
  budget: (agent_id: number | null, metric: string, amount: number | null, reason: string, hours: number | null) =>
    send("POST", "/api/access/budgets", { agent_id, metric, amount, reason, hours }),
  decide: (id: number, decision: "grant" | "deny", note: string) =>
    send("POST", `/api/access/requests/${id}/decide`, { decision, note }),
  resume: (agentId: number, reason: string) => send("POST", `/api/access/agents/${agentId}/resume`, { reason }),
  settings: (changes: Record<string, number>) => send("PUT", "/api/access/settings", changes),
};

/** $1.20 · 1.2M tok · 12 runs; "no limit" for null. */
export function fmtMetric(metric: string, v: number | null | undefined): string {
  if (v == null) return "no limit";
  if (metric.startsWith("usd")) return `$${v.toFixed(2)}`;
  if (metric.startsWith("tokens")) return v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M` : `${Math.round(v / 1000)}k`;
  return String(Math.round(v));
}
