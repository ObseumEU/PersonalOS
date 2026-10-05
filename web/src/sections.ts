import {
  BarChart3,
  Bot,
  Box,
  BookOpen,
  Briefcase,
  CalendarDays,
  CheckSquare,
  FileText,
  FolderKanban,
  FolderOpen,
  Gauge,
  Hash,
  Home,
  KeyRound,
  type LucideIcon,
  MessagesSquare,
  Plug,
  Settings,
  Smartphone,
  Workflow,
  Wrench,
} from "lucide-react";
import { t } from "./i18n";

export type Section = {
  /** The path the item opens. */
  path: string;
  /** i18n key of the label. */
  key: string;
  icon: LucideIcon;
  /** Paths (first segment) that count as this section, for the active state. */
  match: string[];
  /** Shown in the phone tab bar. */
  mobile?: boolean;
  /** Not implemented yet: red mock dot in the navigation; the text is the tooltip. */
  mock?: string;
};

export type SubSection = { path: string; key: string; icon: LucideIcon };

// Seven places: what needs you, how the company is doing, talking, doing, knowing, who, and the rest
// folded under Nastavení.
export const SECTIONS: Section[] = [
  { path: "/today", key: "nav.home", icon: Home, match: ["today", "approvals", "weekly-review", "assistant"], mobile: true },
  { path: "/company", key: "nav.company", icon: Gauge, match: ["company", "goals"] },
  { path: "/chat", key: "nav.chat", icon: MessagesSquare, match: ["chat"], mobile: true },
  { path: "/tasks", key: "nav.work", icon: Briefcase, match: ["tasks", "projects", "calendar", "work"], mobile: true },
  { path: "/knowledge", key: "nav.knowledge", icon: BookOpen, match: ["knowledge", "files", "notes", "topics"] },
  { path: "/team", key: "nav.team", icon: Bot, match: ["team", "agents"], mobile: true },
];

/** Práce: one section, three tabs. */
export const WORK_TABS: SubSection[] = [
  { path: "/tasks", key: "nav.tasks", icon: CheckSquare },
  { path: "/projects", key: "nav.projects", icon: FolderKanban },
  { path: "/calendar", key: "nav.calendar", icon: CalendarDays },
];

/** Znalosti: one list with filters; the full pages stay for details. */
export const KNOWLEDGE_TABS: SubSection[] = [
  { path: "/knowledge", key: "act.all", icon: BookOpen },
  { path: "/files", key: "nav.files", icon: FolderOpen },
  { path: "/notes", key: "nav.notes", icon: FileText },
  { path: "/topics", key: "nav.topics", icon: Hash },
];

/** Nastavení: folded in the rail, a list on its own page and in the phone's "Víc" sheet. */
export const SETTINGS: (SubSection & { blurb: string })[] = [
  { path: "/credentials", key: "nav.credentials", icon: KeyRound, blurb: "settings.blurb.credentials" },
  { path: "/connectors", key: "nav.connectors", icon: Plug, blurb: "settings.blurb.connectors" },
  { path: "/tools", key: "nav.tools", icon: Wrench, blurb: "settings.blurb.tools" },
  { path: "/automations", key: "nav.automations", icon: Workflow, blurb: "settings.blurb.automations" },
  { path: "/system", key: "nav.system", icon: Box, blurb: "settings.blurb.system" },
  { path: "/reports", key: "nav.reports", icon: BarChart3, blurb: "settings.blurb.reports" },
  { path: "/settings/mobile", key: "nav.mobile", icon: Smartphone, blurb: "settings.blurb.mobile" },
];
export const SETTINGS_ROOT = { path: "/settings", key: "nav.settings", icon: Settings };

export function sectionOf(pathname: string): string {
  const first = pathname.split("/")[1] ?? "";
  if (first === "settings" || SETTINGS.some((s) => s.path === `/${first}`)) return "/settings";
  return SECTIONS.find((s) => s.match.includes(first))?.path ?? "";
}

export const navLabel = (s: { key: string }) => t(s.key);
