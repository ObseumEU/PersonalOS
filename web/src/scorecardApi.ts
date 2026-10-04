import { api } from "./api";

/** The company scorecard (pos.scorecard): every number computed by the API. */
export type ScoreKpi = { value: number | null; prev: number | null; delta: number | null; good: boolean | null };

export type ScoreGoal = {
  id: number;
  title: string;
  status: "active" | "proposed";
  owner: string | null;
  metric: string | null;
  baseline: number | null;
  current: number | null;
  target_value: number | null;
  target: string | null;
  due: string | null;
  progress: number;
  current_at: string | null;
  lower_is_better: boolean;
  trend: [string, number | null, number | null][];
  delta: number | null;
  progress_delta: number | null;
};

export type Problem = { text: string; why: string; link: string; score: number };

export type Scorecard = {
  day: string;
  generated_at: string;
  live_at: string;
  compared_with: string | null;
  goals: ScoreGoal[];
  world: {
    outbound: { sent: number | null; replies: number | null; failed: number | null; not_configured: number | null; by_action: Record<string, number>; source: string };
    published: number;
    deploys_ok: number;
    deploys_failed: number;
    customers_helped: number;
  };
  owner: {
    days: number;
    total: number;
    done: number;
    delivered_pct: number | null;
    median_hours: number | null;
    open: number;
    oldest_open: { ref: string; title: string; status: string; assignee: string | null; age_days: number }[];
  };
  spend: {
    business_usd: number;
    platform_usd: number;
    total_usd: number;
    business_share: number | null;
    platform_share: number | null;
    target_share: number;
    platform_cap: number;
    business_outcomes: number;
    usd_per_business_outcome: number | null;
    delivered: number;
    usd_per_delivered: number | null;
  };
  agents: {
    runs_ok: number;
    runs_failed: number;
    runs_blocked: number;
    runs_cancelled: number;
    fail_rate: number | null;
    review_queue: number;
    review_over_sla: number;
    review_oldest_hours: number | null;
    loops: number;
    incidents: number;
    frustrations: number;
    double_answers: number;
    unanswered: number;
  };
  kpis: Record<string, ScoreKpi>;
  problems: Problem[];
};

export const scorecardApi = {
  get: () => api<Scorecard>("/api/scorecard"),
  refresh: () => api<Scorecard>("/api/scorecard/refresh", { method: "POST" }),
};
