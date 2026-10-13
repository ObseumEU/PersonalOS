import { api } from "./api";

/** Tajemství: agents' secrets and the operator's server-store entries (pos.credentials.secrets, /api/secrets).
 * Owner only. Never a value: no endpoint returns one. */
export type SecretHolder = { agent_id: number; name: string; granted_by: string[]; since: string };
export type SecretPending = { approval_id: number; from: string | null; to: string[]; why: string | null; at: string };
export type Secret = {
  name: string;
  description: string;
  purpose: string;
  origin: "agent" | "operator";
  creator: string | null;
  creator_id: number | null;
  team: string | null;
  project: string | null;
  sensitivity: "normal" | "high";
  hosts: string[];
  fields: { name: string; credential: string | null; source: string | null }[];
  credentials: string[];
  holders: SecretHolder[];
  last_use: { at: string; ok: boolean; tool: string; agent: string | null } | null;
  uses_7d: number;
  pending: SecretPending[];
  created_at: string;
  rotated_at: string | null;
  stored: boolean;
};
export type SecretsOverview = {
  secrets: Secret[];
  store: { enabled: boolean; reason: string | null };
  per_agent_limit: number;
  values_shown: false;
};

const post = <T>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
const at = (name: string) => `/api/secrets/${encodeURIComponent(name)}`;

export const secretsApi = {
  overview: () => api<SecretsOverview>("/api/secrets"),
  revoke: (name: string, agentId: number, reason: string) => post(`${at(name)}/revoke`, { agent_id: agentId, reason }),
  sensitivity: (name: string, sensitivity: "normal" | "high") => post(`${at(name)}/sensitivity`, { sensitivity }),
  remove: (name: string, reason: string) => post(`${at(name)}/delete`, { reason }),
};

/** High first, then the ones waiting for an approval, then by name. */
export function sortSecrets(xs: Secret[]): Secret[] {
  const rank = (s: Secret) => (s.sensitivity === "high" ? 0 : 2) - (s.pending.length ? 1 : 0);
  return [...xs].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

/** The search box: name, description, purpose, creator, team, holders, hosts. */
export function matchSecret(s: Secret, q: string): boolean {
  const n = q.trim().toLowerCase();
  if (!n) return true;
  return [s.name, s.description, s.purpose, s.creator ?? "", s.team ?? "", s.project ?? "", ...s.hosts,
    ...s.holders.map((h) => h.name)].some((x) => x.toLowerCase().includes(n));
}
