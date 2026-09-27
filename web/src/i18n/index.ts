/**
 * Czech UI strings: one dictionary, one small helper (no i18n library).
 *
 * t("nav.home") looks a key up; {name} placeholders are filled from vars.
 * A missing key shows the key itself, so a typo is visible on the page.
 * The dictionary is split by area (./cs/*.ts) only to keep files short;
 * the areas are merged here into one map, and keys are unique across them.
 */
import agents from "./cs/agents";
import chat from "./cs/chat";
import common from "./cs/common";
import home from "./cs/home";
import knowledge from "./cs/knowledge";
import settings from "./cs/settings";
import team from "./cs/team";
import work from "./cs/work";

export const cs: Record<string, string> = { ...common, ...home, ...chat, ...agents, ...work, ...knowledge, ...settings, ...team };

export type Vars = Record<string, string | number | null | undefined>;

export function t(key: string, vars?: Vars): string {
  const s = cs[key] ?? key;
  return vars ? s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] == null ? "" : String(vars[k]))) : s;
}

/** Czech plural: plural(3, "úkol", "úkoly", "úkolů") gives "úkoly". */
export function plural(n: number, one: string, few: string, many: string): string {
  const a = Math.abs(n);
  if (a === 1) return one;
  if (a >= 2 && a <= 4) return few;
  return many;
}

export const LOCALE = "cs-CZ";

/** A value from the API (a permission, runtime, status, run kind…) in words; the raw value when unknown. */
export function label(group: string, value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const k = `${group}.${value}`;
  return k in cs ? cs[k] : value.replace(/_/g, " ");
}

/** "před 5 min", "před 2 h", "nikdy". */
export function ago(iso: string | null | undefined): string {
  if (!iso) return t("time.never");
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return t("time.just_now");
  if (s < 3600) return t("time.min_ago", { n: Math.round(s / 60) });
  if (s < 86400) return t("time.h_ago", { n: Math.round(s / 3600) });
  return t("time.d_ago", { n: Math.round(s / 86400) });
}

export function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
}

export function fmtDateTime(iso: string): string {
  return new Date(iso).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
}
