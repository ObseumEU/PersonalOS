import {
  Bot,
  CalendarDays,
  CheckSquare,
  FileText,
  FolderOpen,
  Hash,
  type LucideIcon,
  Settings2,
  Sun,
} from "lucide-react";

export type Section = {
  path: string;
  label: string;
  icon: LucideIcon;
  phase: number;
  group: "daily" | "assistant" | "system";
  blurb: string;
  features: string[];
};

// Sections from docs/PLAN.md §3. Each gets its real page in a later phase.
export const SECTIONS: Section[] = [
  {
    path: "today",
    label: "Today",
    icon: Sun,
    phase: 4,
    group: "daily",
    blurb: "Your day at a glance.",
    features: ["Today's calendar", "Tasks due soon", "Recent files", "Ask anything"],
  },
  {
    path: "tasks",
    label: "Tasks",
    icon: CheckSquare,
    phase: 2,
    group: "daily",
    blurb: "Everything you need to get done, by topic and due date.",
    features: ["Quick-add", "Due dates and priority", "Grouped by topic", "Created by the assistant"],
  },
  {
    path: "calendar",
    label: "Calendar",
    icon: CalendarDays,
    phase: 4,
    group: "daily",
    blurb: "Your Google or Microsoft 365 calendar, next to your tasks.",
    features: ["Synced events", "Day and week view", "Tasks alongside events"],
  },
  {
    path: "files",
    label: "Files",
    icon: FolderOpen,
    phase: 2,
    group: "daily",
    blurb: "Every document in one place, searchable in full text.",
    features: ["Drag-and-drop upload", "Preview", "Tags and topics", "Full-text search"],
  },
  {
    path: "topics",
    label: "Topics",
    icon: Hash,
    phase: 2,
    group: "daily",
    blurb: "One home for each area of your life and work.",
    features: ["Files, notes and tasks together", "Events and conversations", "Clients, projects, health, house"],
  },
  {
    path: "notes",
    label: "Notes",
    icon: FileText,
    phase: 2,
    group: "daily",
    blurb: "Markdown notes that belong to your topics.",
    features: ["Markdown editor", "Linked to topics", "Searchable"],
  },
  {
    path: "assistant",
    label: "Assistant",
    icon: Bot,
    phase: 3,
    group: "assistant",
    blurb: "Ask about your files, tasks and calendar. Answers link to their sources.",
    features: ["Runs on Codex CLI", "Cites files, tasks and events", "Delegates deep research to subsystems"],
  },
  {
    path: "admin",
    label: "Admin",
    icon: Settings2,
    phase: 5,
    group: "system",
    blurb: "Subsystems, connected accounts and settings.",
    features: ["Subsystem health", "Links to Nexus and Knowledge agent", "Codex login", "Connected accounts"],
  },
];

export const bySlug = (path: string) => SECTIONS.find((s) => s.path === path)!;
