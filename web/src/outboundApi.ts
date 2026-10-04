import { api } from "./api";

export type EmailPolicy = {
  mode: "draft" | "auto";
  auto_domains: string[];
  configured: Record<string, { draft: boolean; send: boolean }>;
  linkedin?: { app: boolean; connected: boolean; name?: string | null; redirect_uri: string };
};

export type DraftTrust = {
  drafted: number;
  waiting: number;
  sent_unchanged: number;
  sent_edited: number;
  discarded: number;
  resolved: number;
  unchanged_rate: number | null;
  median_minutes_to_send: number | null;
  ready_for_auto: boolean;
  advice: string;
};

export const outboundApi = {
  policy: () => api<EmailPolicy>("/api/outbound/policy"),
  setMode: (mode: "draft" | "auto", auto_domains?: string[]) =>
    api<EmailPolicy>("/api/outbound/policy", { method: "PUT", body: JSON.stringify({ mode, auto_domains }) }),
  drafts: () => api<{ waiting: { id: number; to: string; subject: string; link: string }[]; trust: DraftTrust }>("/api/outbound/drafts"),
  publishLinkedIn: (approvalId: number) =>
    api<{ status: string; url?: string }>(`/api/integrations/linkedin/publish/${approvalId}`, { method: "POST", body: "{}" }),
};
