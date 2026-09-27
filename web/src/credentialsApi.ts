import { api } from "./api";
import type { AccessRequest } from "./accessApi";

/** Credentials backed by 1Password (pos.credentials, /api/credentials). Never values. */
export type CredGrant = {
  id: number;
  agent_id: number;
  agent_name: string;
  capability: string;
  credential: string;
  scope: string | null;
  granted_by_name: string | null;
  reason: string;
  created_at: string;
  expires_at: string | null;
  ended_at: string | null;
  end_kind: string | null;
  end_reason: string | null;
  active: boolean;
};

export type Credential = {
  id: number;
  name: string;
  op_ref: string;
  description: string;
  env_var: string | null;
  header: string | null;
  allowed_hosts: string[];
  allowed_tools: string[];
  allowed_commands: string[];
  max_uses_hour: number;
  notes: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  grants?: CredGrant[];
  uses_24h?: number;
  errors_24h?: number;
  last_use?: CredLast | null;
  last_test?: CredLast | null;
  kind?: CredKind;
  item?: string;
  companions?: string[];
  recommended?: AgentPick[];
};

export type CredKind = "ssh" | "token" | "basic" | "db" | "generic";
export type CredLast = { at: string; ok: boolean; error: string | null; agent?: string | null; tool?: string };
export type AgentPick = { id: number; name: string; why: string };
export type RosterAgent = { id: number; name: string; role: string | null; team: string | null; purpose: string };

/** One line of the grouped audit: a day, a credential, an agent. */
export type AuditLine = {
  day: string;
  name: string;
  credential_id: number | null;
  agent_id: number | null;
  agent_name: string | null;
  count: number;
  errors: number;
  first_at: string;
  last_at: string;
  tools: string[];
  hosts: string[];
  last_error: string | null;
};

export type SuggestedCred = {
  field: string | null;
  field_id: string | null;
  op_ref: string;
  role: string;
  name: string;
  env_var: string | null;
  header: string | null;
  allowed_hosts: string[];
  allowed_tools: string[];
  allowed_commands: string[];
  description: string;
};

/** A vault item that is not registered yet, with what PersonalOS suggests doing with it. */
export type Suggestion = {
  item_id: string;
  title: string;
  category: string;
  kind: CredKind;
  kind_label: string;
  decided: boolean;
  source: "rules" | "llm" | "owner";
  why: string;
  known: string | null;
  usage: string;
  hosts: string[];
  credentials: SuggestedCred[];
  extra_grants: string[];
  agents: AgentPick[];
  fields: { id: string; title: string; type: string; section: string | null; op_ref: string }[];
  hidden?: boolean;
};

export type Discovery = CredStatus & { items: Suggestion[]; hidden: { item_id: string; title: string }[]; error: string | null };

export type CredRequest = AccessRequest & {
  credential: string;
  scope: string | null;
  registered: boolean;
  credential_id: number | null;
  suggestion: Suggestion | null;
};

export type CredUse = {
  id: number;
  at: string;
  name: string;
  agent_id: number | null;
  agent_name: string | null;
  run_id: number | null;
  task_ref: string | null;
  tool: string;
  host: string | null;
  ok: boolean;
  error: string | null;
};

export type CredStatus = { enabled: boolean; vault: string | null; reason: string | null; cache_seconds: number };

export type CredOverview = CredStatus & {
  credentials: Credential[];
  requests: CredRequest[];
  paused: CredGrant[];
  agents: RosterAgent[];
  audit: AuditLine[];
};

export type VaultField = { id: string; title: string; type: string; section: string | null; op_ref: string; registered: boolean };
export type VaultItem = { id: string; title: string; category: string; fields: VaultField[] };

export type CredentialIn = Partial<Pick<Credential, "name" | "op_ref" | "description" | "env_var" | "header" | "notes" | "max_uses_hour">> & {
  allowed_hosts?: string[];
  allowed_tools?: string[];
  allowed_commands?: string[];
};

const send = <T,>(method: string, path: string, body: unknown) => api<T>(path, { method, body: JSON.stringify(body) });

export const credentialsApi = {
  overview: () => api<CredOverview>("/api/credentials"),
  detail: (id: number) => api<Credential & { grants: CredGrant[]; uses: CredUse[] }>(`/api/credentials/${id}`),
  vault: () => api<{ vault: string; items: VaultItem[] }>("/api/credentials/vault"),
  add: (c: CredentialIn) => send<Credential>("POST", "/api/credentials", c),
  update: (id: number, c: CredentialIn) => send<Credential>("PATCH", `/api/credentials/${id}`, c),
  archive: (id: number, reason: string) => send("POST", `/api/credentials/${id}/archive`, { reason }),
  test: (id: number) => send<{ ok: boolean; error: string | null }>("POST", `/api/credentials/${id}/test`, {}),
  grant: (agent_id: number, name: string, reason: string, hours: number | null, scope: string | null) =>
    send("POST", "/api/credentials/grants", { agent_id, name, reason, hours, scope }),
  revoke: (grantId: number, reason: string) => send("POST", `/api/credentials/grants/${grantId}/revoke`, { reason }),
  resume: (grantId: number) => send("POST", `/api/credentials/grants/${grantId}/resume`, { reason: "" }),
  decide: (requestId: number, decision: "grant" | "deny", note: string, hours: number | null = null) =>
    send("POST", `/api/credentials/requests/${requestId}/decide`, { decision, note, hours }),
  agent: (agentId: number) =>
    api<CredStatus & { grants: CredGrant[]; uses: CredUse[]; audit: AuditLine[]; requests: CredRequest[]; available: string[] }>(`/api/credentials/agents/${agentId}`),
  discover: (refresh = false, hidden = false) =>
    api<Discovery>(`/api/credentials/discover?refresh=${refresh}&hidden=${hidden}`),
  suggest: (itemId: string, kind: CredKind | null) => send<Suggestion>("POST", `/api/credentials/discover/${encodeURIComponent(itemId)}/suggest`, { kind }),
  dismiss: (itemId: string, hidden: boolean) => send("POST", `/api/credentials/discover/${encodeURIComponent(itemId)}/dismiss`, { hidden }),
  register: (body: { item_id: string | null; credentials: SuggestedCred[]; agent_ids: number[]; hours: number | null; reason?: string }) =>
    send<{ credentials: Credential[]; grants: number[]; extra_grants: string[] }>("POST", "/api/credentials/register", body),
  grantMany: (agent_ids: number[], names: string[], hours: number | null, scope: string | null, reason = "") =>
    send<{ grants: number[] }>("POST", "/api/credentials/grants/bulk", { agent_ids, names, hours, scope, reason }),
  revokeMany: (grant_ids: number[], reason = "") => send<{ revoked: number[] }>("POST", "/api/credentials/grants/revoke", { grant_ids, reason }),
  restoreMany: (grant_ids: number[]) => send<{ grants: number[] }>("POST", "/api/credentials/grants/restore", { grant_ids, reason: "" }),
  uses: (q: { name?: string; agent_id?: number | null; day?: string }) => {
    const p = new URLSearchParams();
    if (q.name) p.set("name", q.name);
    if (q.agent_id != null) p.set("agent_id", String(q.agent_id));
    if (q.day) p.set("day", q.day);
    return api<{ uses: CredUse[] }>(`/api/credentials/uses?${p}`);
  },
};
