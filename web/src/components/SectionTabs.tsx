import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { t } from "../i18n";
import type { SubSection } from "../sections";

/** Tabs over a section's pages (Práce: Úkoly · Projekty · Kalendář; Znalosti: filters). */
export default function SectionTabs({ tabs, label, children }: { tabs: SubSection[]; label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <nav aria-label={label} className="-mx-1 flex gap-1 overflow-x-auto overflow-y-hidden border-b border-line px-1">
        {tabs.map((s) => (
          <NavLink
            key={s.path}
            to={s.path}
            end={s.path === "/knowledge"}
            className={({ isActive }) =>
              `-mb-px shrink-0 border-b-2 px-3 py-2 text-sm whitespace-nowrap ${
                isActive ? "border-accent text-ink" : "border-transparent text-ink-2 hover:text-ink"
              }`
            }
          >
            {t(s.key)}
          </NavLink>
        ))}
      </nav>
      {children}
    </div>
  );
}
