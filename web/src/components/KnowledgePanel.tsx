import { t } from "../i18n";
import { type KGraph, useKnowledgeGraph } from "../knowledgeApi";
import KnowledgeGraph from "./LazyGraph";
import { Legend, Panel } from "./ui";

/** Our knowlage as a 3D graph: workspaces → sources → collections → newest documents. */
export default function KnowledgePanel({
  title = t("kg.title"),
  className = "",
  highlight,
  period,
  graph: given,
}: {
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
    t("act.loading")
  ) : graph.available ? (
    <a href={graph.url} target="_blank" rel="noreferrer" className="hover:text-accent">
      {t("kg.stats", {
        docs: graph.stats.documents?.toLocaleString("cs-CZ"),
        cols: graph.stats.collections,
        cached: graph.stale ? t("kg.cached") : "",
      })}
    </a>
  ) : (
    t("kg.down")
  );
  return (
    <Panel title={title} right={right} className={className} bodyClassName="measure-grid relative">
      {graph?.available && graph.nodes.length > 0 && <KnowledgeGraph data={graph} highlight={highlight} period={period} />}
      {graph && !graph.available && (
        <div className="absolute inset-0 grid place-items-center p-6 text-center">
          <p className="max-w-sm text-sm leading-relaxed text-ink-2">{t("kg.down_long", { url: graph.url || "knowlage", error: graph.error })}</p>
        </div>
      )}
      {graph?.available && (
        <>
          <div className="pointer-events-none absolute bottom-3 left-4">
            <Legend
              items={[
                ["#6cc4dc", highlight?.length ? t("kg.workspace_cited") : t("kg.workspace")],
                ["#e6e8eb", t("kg.source")],
                ["#7d848f", t("kg.docs")],
              ]}
            />
          </div>
          <span className="pointer-events-none absolute right-4 bottom-3 hidden text-xs text-ink-2 sm:block">{t("kg.hint")}</span>
        </>
      )}
    </Panel>
  );
}
