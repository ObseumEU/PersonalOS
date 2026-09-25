import { lazy, Suspense, type ComponentProps } from "react";

// three.js is large; load it as its own chunk only where a graph is shown.
const KnowledgeGraph = lazy(() => import("./KnowledgeGraph"));

export default function LazyGraph(props: ComponentProps<typeof KnowledgeGraph>) {
  return (
    <Suspense fallback={<p className="cap breathe absolute inset-0 grid place-items-center">loading graph…</p>}>
      <KnowledgeGraph {...props} />
    </Suspense>
  );
}
