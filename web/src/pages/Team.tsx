import { Link, useSearchParams } from "react-router-dom";
import { PageHeader } from "../components/ui";
import { t } from "../i18n";
import Agents from "./Agents";
import Board from "./Board";
import Org from "./Org";

// Tým: people and agents, their work, the structure (the old network graph is now the structure list).
const TABS = [
  { id: "people", key: "team.tab.people", Page: Agents },
  { id: "work", key: "team.tab.work", Page: Board },
  { id: "structure", key: "team.tab.structure", Page: Org },
] as const;

export default function Team() {
  const [params, setParams] = useSearchParams();
  const tab = TABS.find((x) => x.id === params.get("tab")) ?? (params.get("tab") === "network" ? TABS[2] : TABS[0]);
  return (
    <div className="flex flex-col gap-4">
      <PageHeader kicker={t("team.kicker")} title={t("nav.team")} sub={t("team.sub")} />
      <nav className="-mx-1 flex gap-1 overflow-x-auto overflow-y-hidden border-b border-line px-1" aria-label={t("nav.team")}>
        {TABS.map((x) => (
          <button
            key={x.id}
            type="button"
            aria-current={x.id === tab.id ? "page" : undefined}
            onClick={() => setParams(x.id === "people" ? {} : { tab: x.id })}
            className={`-mb-px shrink-0 border-b-2 px-3 py-2 text-sm whitespace-nowrap ${x.id === tab.id ? "border-accent text-ink" : "border-transparent text-ink-2 hover:text-ink"}`}
          >
            {t(x.key)}
          </button>
        ))}
        {/* HR (pos.hr): scores, proposals, admissions, restore: its own page. */}
        <Link to="/hr" className="-mb-px ml-auto shrink-0 border-b-2 border-transparent px-3 py-2 text-sm whitespace-nowrap text-ink-2 hover:text-accent">
          {t("hr.link")}
        </Link>
      </nav>
      <tab.Page />
    </div>
  );
}
