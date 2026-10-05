/* The goal form (pages/Goals.tsx) as plain data: what a goal fills it with, and the body for
 * POST /api/goals or PATCH /api/goals/{id} (pos.goals). Pure, so it is tested without a browser. */

export type GoalLike = {
  title: string;
  why: string;
  target: string;
  owner_name: string | null;
  due: string | null;
  status: string;
  progress: number | null;
  parent_id: number | null;
  metric?: string | null;
  baseline?: number | null;
  current?: number | null;
  target_value?: number | null;
};

export type GoalForm = {
  title: string;
  why: string;
  target: string;
  owner: string;
  due: string;
  status: string;
  progress: string;
  parent_id: string;
  metric: string;
  baseline: string;
  current: string;
  target_value: string;
};

const s = (v: number | string | null | undefined) => (v == null ? "" : String(v));

export function formOf(g?: GoalLike | null): GoalForm {
  return {
    title: g?.title ?? "",
    why: g?.why ?? "",
    target: g?.target ?? "",
    owner: g?.owner_name ?? "",
    due: g?.due ?? "",
    status: g?.status ?? "active",
    progress: s(g?.progress),
    parent_id: s(g?.parent_id),
    metric: g?.metric ?? "",
    baseline: s(g?.baseline),
    current: s(g?.current),
    target_value: s(g?.target_value),
  };
}

/** "12,5" (Czech decimal comma) and "12.5" are numbers; an empty field is null; anything else is NaN. */
export function parseNumber(v: string): number | null {
  const x = v.trim().replace(/\s/g, "").replace(",", ".");
  if (!x) return null;
  return /^-?\d+(\.\d+)?$/.test(x) ? Number(x) : NaN;
}

/**
 * The request body. A new goal sends only what is filled in; an edit sends only what changed (an emptied
 * field as null, so it is cleared). Returns the name of the first bad number field instead, when there is one.
 */
export function goalBody(f: GoalForm, before?: GoalForm): Record<string, unknown> | { invalid: string } {
  const out: Record<string, unknown> = {};
  const text = ["title", "why", "target", "owner", "due", "metric"] as const;
  for (const k of text) {
    const v = f[k].trim();
    if (before ? v !== before[k].trim() : v) out[k] = v || null;
  }
  if (before ? f.status !== before.status : f.status !== "active") out.status = f.status;
  for (const k of ["progress", "baseline", "current", "target_value", "parent_id"] as const) {
    if (before ? f[k].trim() === before[k].trim() : !f[k].trim()) continue;
    const n = parseNumber(f[k]);
    if (Number.isNaN(n)) return { invalid: k };
    if (k === "progress" && n != null && (n < 0 || n > 100 || !Number.isInteger(n))) return { invalid: k };
    out[k] = n;
  }
  return out;
}
