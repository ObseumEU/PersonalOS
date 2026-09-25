import {
  Box,
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
};

// Sections from docs/PLAN.md §3.
export const SECTIONS: Section[] = [
  { path: "today", label: "Today", icon: Sun, phase: 4, mobile: true, blurb: "Your day at a glance.", features: [] },
  {
    path: "tasks", label: "Tasks", icon: CheckSquare, phase: 2, mobile: true,
    blurb: "Everything you need to get done, by topic and due date.",
    features: ["Quick-add", "Due dates and priority", "Grouped by topic", "Created by the assistant"],
  },
  {
    path: "calendar", label: "Calendar", icon: CalendarDays, phase: 4, mobile: true,
    blurb: "Your Google or Microsoft 365 calendar, next to your tasks.",
    features: ["Synced events", "Day and week view", "Tasks alongside events"],
  },
  {
    path: "files", label: "Files", icon: FolderOpen, phase: 2, mobile: true,
    blurb: "Every document in one place, searchable in full text.",
    features: ["Drag-and-drop upload", "Preview", "Tags and topics", "Full-text search"],
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
  { path: "assistant", label: "Assistant", icon: Orbit, phase: 3, mobile: true, blurb: "", features: [] },
  { path: "system", label: "System", icon: Box, phase: 5, blurb: "", features: [] },
];
