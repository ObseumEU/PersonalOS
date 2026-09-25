import { useEffect, useState } from "react";
import { api } from "./api";

export type KNode = {
  id: string;
  label: string;
  type: "workspace" | "source" | "collection" | "document";
  workspace: string | null;
  source: string | null;
  url: string | null;
  weight: number;
};
export type KEdge = { source: string; target: string; type: string; weight: number };
export type KGraph = {
  available: boolean;
  url: string;
  error?: string;
  stale?: boolean;
  nodes: KNode[];
  edges: KEdge[];
  stats: { documents?: number; workspaces?: number; sources?: number; collections?: number };
};

export type Passage = { title: string; date: string | null; location: string; url: string | null; quote: string; doc_id: string | null; kind: string };
export type Citation = { n: number; claim: string; type: string; verified: boolean; passages: Passage[] };
export type Answer = {
  ok: boolean;
  error?: string;
  limit?: boolean;
  answer?: string;
  citations?: Citation[];
  verified?: boolean;
  steps?: number;
  thread_id?: string | null;
  url?: string;
};

export const knowledgeApi = {
  graph: (docs = 3) => api<KGraph>(`/api/knowledge/graph?docs=${docs}`),
  ask: (question: string, workspace?: string) =>
    api<Answer>("/api/knowledge/ask", { method: "POST", body: JSON.stringify({ question, workspace }) }),
};

/** The knowledge graph, shared by every screen that shows it (fetched once per page load). */
let pending: Promise<KGraph> | null = null;
export function useKnowledgeGraph() {
  const [graph, setGraph] = useState<KGraph | null>(null);
  useEffect(() => {
    let live = true;
    pending ??= knowledgeApi.graph().catch((e) => ({ available: false, url: "", error: String(e.message ?? e), nodes: [], edges: [], stats: {} }));
    pending.then((g) => live && setGraph(g));
    return () => {
      live = false;
    };
  }, []);
  return graph;
}

export type Subsystem = { name: string; proto: string; ok: boolean; value: number | null; unit: string; detail: string; url: string | null };

/** Live state of knowlage, Nexus and the runtimes (refreshed every minute). */
export function useSubsystems() {
  const [items, setItems] = useState<Subsystem[] | null>(null);
  useEffect(() => {
    const load = () => api<Subsystem[]>("/api/system/subsystems").then(setItems, () => setItems([]));
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);
  return items;
}
