import {
  Bot,
  Workflow,
  Wrench,
  Plug,
  Box,
  Share2,
  ShieldCheck,
  CalendarDays,
  CheckSquare,
  FileText,
  FolderOpen,
  Hash,
  type LucideIcon,
  Orbit,
  Sun,
} from "lucide-react";

export type Section = {
  path: string;
  label: string;
  icon: LucideIcon;
  phase: number;
  blurb: string;
  features: string[];
  /** Shown in the phone tab bar. */
  mobile?: boolean;
  /** Not implemented yet: red mock dot in the navigation; the text is the tooltip. */
  mock?: string;
};

// Sections from docs/PLAN.md §3.
export const SECTIONS: Section[] = [
  { path: "today", label: "Today", icon: Sun, phase: 4, mobile: true, blurb: "Your day at a glance.", features: [] },
  {
    path: "tasks", label: "Tasks", icon: CheckSquare, phase: 1, mobile: true,
    blurb: "Everything you need to get done, by topic and due date.",
    features: ["Quick-add", "Due dates and priority", "Grouped by topic", "Created by the assistant"],
  },
  {
    path: "calendar", mock: "planned screen, no calendar sync yet", label: "Calendar", icon: CalendarDays, phase: 4,
    blurb: "Your Google or Microsoft 365 calendar, next to your tasks.",
    features: ["Synced events", "Day and week view", "Tasks alongside events"],
  },
  {
    path: "files", mock: "planned screen, no file storage yet", label: "Files", icon: FolderOpen, phase: 2,
    blurb: "Every document in one place, searchable in full text.",
    features: ["Drag-and-drop upload", "Preview", "Tags and topics", "Full-text search"],
  },
  {
    path: "topics", mock: "planned screen, not built yet", label: "Topics", icon: Hash, phase: 2,
    blurb: "One home for each area of your life and work.",
    features: ["Files, notes and tasks together", "Events and conversations"],
  },
  {
    path: "notes", mock: "planned screen, not built yet", label: "Notes", icon: FileText, phase: 2,
    blurb: "Markdown notes that belong to your topics.",
    features: ["Markdown editor", "Linked to topics", "Searchable"],
  },
  { path: "assistant", label: "Assistant", icon: Orbit, phase: 3, blurb: "", features: [] },
  { path: "agents", label: "Agents", icon: Bot, phase: 2, mobile: true, blurb: "", features: [] },
  { path: "network", label: "Network", icon: Share2, phase: 2, blurb: "", features: [] },
  { path: "approvals", label: "Approvals", icon: ShieldCheck, phase: 2, mobile: true, blurb: "", features: [] },
  { path: "automations", label: "Automations", icon: Workflow, phase: 5, blurb: "", features: [] },
  { path: "tools", label: "Tools", icon: Wrench, phase: 5, blurb: "", features: [] },
  { path: "connectors", label: "Connectors", icon: Plug, phase: 4, blurb: "", features: [] },
  { path: "system", label: "System", icon: Box, phase: 5, blurb: "", features: [] },
];
