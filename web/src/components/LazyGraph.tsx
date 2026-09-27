import { lazy, Suspense, type ComponentProps } from "react";
import { t } from "../i18n";

// three.js is large; load it as its own chunk only where a graph is shown.
const KnowledgeGraph = lazy(() => import("./KnowledgeGraph"));

export default function LazyGraph(props: ComponentProps<typeof KnowledgeGraph>) {
  return (
    <Suspense fallback={<p className="breathe absolute inset-0 grid place-items-center text-sm text-ink-2">{t("kg.loading")}</p>}>
      <KnowledgeGraph {...props} />
    </Suspense>
  );
}
