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
  requests: AccessRequest[];
  paused: CredGrant[];
  uses: CredUse[];
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
    api<CredStatus & { grants: CredGrant[]; uses: CredUse[]; available: string[] }>(`/api/credentials/agents/${agentId}`),
};
