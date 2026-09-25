// SAMPLE DATA. Placeholder content until Phase 2 adds real tasks, files and
// calendar sync. Every screen that shows it labels it as sample data.

export type SampleTask = { id: string; title: string; topic: string; due: string; urgent: boolean };
export type SampleEvent = { start: number; end: number; title: string; meta: string };

export const TASKS: SampleTask[] = [
  { id: "T-014", title: "Send the signed contract to Acme", topic: "Acme", due: "today", urgent: true },
  { id: "T-015", title: "Pay the September invoice", topic: "Finance", due: "today", urgent: true },
  { id: "T-021", title: "Draft PersonalOS phase 2 scope", topic: "PersonalOS", due: "tomorrow", urgent: false },
  { id: "T-009", title: "Book the car service", topic: "House", due: "29 Sep", urgent: false },
];

// Hours as decimals (9.5 = 09:30).
export const EVENTS: SampleEvent[] = [
  { start: 8.5, end: 9, title: "Morning review", meta: "Personal" },
  { start: 9.5, end: 9.75, title: "Stand-up", meta: "Obseum" },
  { start: 11, end: 12, title: "Client call: Acme", meta: "Acme" },
  { start: 14, end: 14.75, title: "Dentist", meta: "Health" },
  { start: 16.5, end: 17.25, title: "Nexus review", meta: "Obseum" },
];

export const GRAPH_LABELS = [
  "Acme", "Framework agreement", "Client call", "Stand-up", "Invoice 2026-09", "PersonalOS",
  "Dentist", "House insurance", "Nexus", "Knowledge agent", "Finance", "Health",
];

export const ANSWER = {
  query: "What’s open for Acme this week?",
  summary: "Three items are open for Acme this week. The contract is the only one due today.",
  items: [
    { text: "Send the signed contract to Acme. Due today, marked high priority.", ref: 1 },
    { text: "Client call on Friday at 11:00. The invite lists the Q4 scope as the main topic.", ref: 2 },
    { text: "Decide on renewal by 1 October. The agreement renews on 1 November with a 30-day notice period.", ref: 3 },
  ],
  refs: [
    "Task T-014, “Send the signed contract to Acme”, due 25 Sep",
    "Calendar, “Client call: Acme”, Fri 25 Sep 11:00–12:00",
    "Acme framework agreement.pdf, p. 3, §7",
  ],
  trace: [
    { step: "Full-text search", via: "pos · fts5", time: "12 ms" },
    { step: "List tasks #acme", via: "pos · mcp", time: "8 ms" },
    { step: "Calendar lookup", via: "pos · mcp", time: "15 ms" },
    { step: "Read agreement p. 3", via: "pos · mcp", time: "41 ms" },
    { step: "Compose answer", via: "codex", time: "2.7 s" },
  ],
};

export const SUBSYSTEMS = [
  { name: "Knowledge agent", proto: "A2A", value: "42", unit: "ms p50", detail: "3 workspaces · 128 documents indexed" },
  { name: "Nexus", proto: "A2A facade", value: "88", unit: "ms p50", detail: "12 automations · 1 approval waiting" },
  { name: "Codex CLI", proto: "runtime", value: "2.7", unit: "s / answer", detail: "ChatGPT subscription · no paid API" },
];
