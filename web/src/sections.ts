import {
  BarChart3,
  Bot,
  Workflow,
  Wrench,
  Plug,
  Box,
  ShieldCheck,
  CalendarDays,
  CheckSquare,
  FileText,
  FolderOpen,
  FolderKanban,
  Hash,
  type LucideIcon,
  MessagesSquare,
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
    path: "calendar", label: "Calendar", icon: CalendarDays, phase: 4,
    blurb: "Your Google or Microsoft 365 calendar, next to your tasks.",
    features: ["Synced events", "Day and week view", "Tasks alongside events"],
  },
  {
    path: "files", label: "Files", icon: FolderOpen, phase: 2,
    blurb: "Every document in one place, searchable in full text.",
    features: ["Drag-and-drop upload", "Preview", "Tags and topics", "Full-text search"],
  },
  {
    path: "projects", label: "Projects", icon: FolderKanban, phase: 2,
    blurb: "Shared work with a goal, a lead, members and a channel.",
    features: ["Board", "Lead reviews", "Project channel"],
  },
  {
    path: "topics", label: "Topics", icon: Hash, phase: 2,
    blurb: "One home for each area of your life and work.",
    features: ["Files, notes and tasks together", "Events and conversations"],
  },
  {
    path: "notes", label: "Notes", icon: FileText, phase: 2,
    blurb: "Markdown notes that belong to your topics.",
    features: ["Markdown editor", "Linked to topics", "Searchable"],
  },
  {
    path: "reports", label: "Reports", icon: BarChart3, phase: 5,
    blurb: "The weekly company report and the Friday meeting with the Chief of Staff.",
    features: [],
  },
  { path: "assistant", label: "Assistant", icon: Orbit, phase: 3, blurb: "", features: [] },
  { path: "chat", label: "Chat", icon: MessagesSquare, phase: 2, mobile: true, blurb: "", features: [] },
  { path: "team", label: "Team", icon: Bot, phase: 2, mobile: true, blurb: "People and agents, their work, the structure.", features: [] },
  { path: "approvals", label: "Approvals", icon: ShieldCheck, phase: 2, mobile: true, blurb: "", features: [] },
  { path: "automations", label: "Automations", icon: Workflow, phase: 5, blurb: "", features: [] },
  { path: "tools", label: "Tools", icon: Wrench, phase: 5, blurb: "", features: [] },
  { path: "connectors", label: "Connectors", icon: Plug, phase: 4, blurb: "", features: [] },
  { path: "system", label: "System", icon: Box, phase: 5, blurb: "", features: [] },
];
