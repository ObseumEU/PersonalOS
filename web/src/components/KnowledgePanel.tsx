import { type KGraph, useKnowledgeGraph } from "../knowledgeApi";
import KnowledgeGraph from "./LazyGraph";
import { Legend, Panel } from "./ui";

/** Our knowlage as a 3D graph: workspaces → sources → collections → newest documents. */
export default function KnowledgePanel({
  fig,
  title = "Knowledge graph",
  className = "",
  highlight,
  period,
  graph: given,
}: {
  fig: string;
  title?: string;
  className?: string;
  highlight?: string[];
  period?: number;
  /** A graph already loaded by the page; otherwise it is fetched. */
  graph?: KGraph | null;
}) {
  const loaded = useKnowledgeGraph();
  const graph = given ?? loaded;
  const right = !graph ? (
    "loading…"
  ) : graph.available ? (
    <a href={graph.url} target="_blank" rel="noreferrer" className="hover:text-accent">
      {graph.stats.documents?.toLocaleString("cs-CZ")} documents · {graph.stats.collections} collections · knowlage{graph.stale ? " (cached)" : ""} ↗
    </a>
  ) : (
    "knowlage unreachable"
  );
  return (
    <Panel fig={fig} title={title} right={right} className={className} bodyClassName="measure-grid relative">
      {graph?.available && graph.nodes.length > 0 && <KnowledgeGraph data={graph} highlight={highlight} period={period} />}
      {graph && !graph.available && (
        <div className="absolute inset-0 grid place-items-center p-6 text-center">
          <p className="cap max-w-sm leading-relaxed">
            The knowledge base at {graph.url || "knowlage"} did not answer. {graph.error}
          </p>
        </div>
      )}
      {graph?.available && (
        <>
          <div className="pointer-events-none absolute bottom-3 left-4">
            <Legend
              items={[
                ["#6cc4dc", highlight?.length ? "workspace · cited" : "workspace"],
                ["#e6e8eb", "source · collection"],
                ["#4a515b", "newest documents"],
              ]}
            />
          </div>
          <span className="cap pointer-events-none absolute right-4 bottom-3 hidden sm:block">drag to orbit · click to open</span>
        </>
      )}
    </Panel>
  );
}
