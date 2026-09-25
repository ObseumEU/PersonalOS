import { MockDot, PageHeader, Panel } from "../components/ui";
import type { Section } from "../sections";

export default function ComingSoon({ section }: { section: Section }) {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={`${section.label.toUpperCase()} · PHASE ${section.phase}`} title={section.label} sub={
          <span className="flex flex-wrap items-center gap-2">
            {section.blurb} <MockDot why={section.mock} />
          </span>
        }
      />
      <Panel fig="SPEC" title="Planned" mock="nothing here works yet" right={`arrives in phase ${section.phase}`} className="max-w-xl">
        <ol>
          {section.features.map((f, i) => (
            <li key={f} className="grid grid-cols-[28px_minmax(0,1fr)_auto] items-center border-b border-line px-4 py-2.5 text-sm last:border-0">
              <span className="cap pt-0.5 text-accent!">{String(i + 1).padStart(2, "0")}</span>
              {f}
              <MockDot why="not implemented yet" />
            </li>
          ))}
        </ol>
      </Panel>
    </div>
  );
}
