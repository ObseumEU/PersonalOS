import { useSearchParams } from "react-router-dom";
import Agents from "./Agents";
import Board from "./Board";
import NetworkPage from "./Network";
import Org from "./Org";

// One screen for the team (REVIZE-FUNKCI 4.2): people and agents, their work, the structure, the network.
const TABS = [
  { id: "people", label: "People and agents", Page: Agents },
  { id: "work", label: "Work", Page: Board },
  { id: "structure", label: "Structure", Page: Org },
  { id: "network", label: "Network", Page: NetworkPage },
] as const;

export default function Team() {
  const [params, setParams] = useSearchParams();
  const tab = TABS.find((t) => t.id === params.get("tab")) ?? TABS[0];
  return (
    <div className="flex flex-col gap-4">
      <nav
        className="flex flex-wrap gap-1 border-b border-line"
        aria-label="Team"
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setParams(t.id === "people" ? {} : { tab: t.id })}
            className={`-mb-px border-b-2 px-3 py-2 text-[13px] ${t.id === tab.id ? "border-accent text-ink" : "border-transparent text-ink-3 hover:text-ink"}`}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <tab.Page />
    </div>
  );
}
