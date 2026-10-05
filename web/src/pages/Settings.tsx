import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";
import FreezeCard from "../components/agents/FreezeCard";
import EmailModeCard from "../components/outbound/EmailModeCard";
import { PageHeader } from "../components/ui";
import { t } from "../i18n";
import { SETTINGS } from "../sections";

/** Nastavení: everything that is set up once and rarely touched, plus the kill switch. */
export default function Settings() {
  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("settings.kicker")} title={t("nav.settings")} sub={t("settings.sub")} />
      <EmailModeCard />
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {SETTINGS.map((s) => {
          const Icon = s.icon;
          return (
            <Link key={s.path} to={s.path} className="panel flex items-start gap-3 p-4 transition hover:border-accent">
              <Icon size={20} strokeWidth={1.5} className="mt-0.5 shrink-0 text-accent" />
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="text-sm font-medium">{t(s.key)}</span>
                <span className="text-[13px] leading-snug text-ink-2">{t(s.blurb)}</span>
              </span>
              <ChevronRight size={16} className="mt-0.5 shrink-0 text-ink-2" />
            </Link>
          );
        })}
      </div>
      {/* The kill switch: folded at the bottom, behind a confirmation. */}
      <details className="rounded-lg border border-red-400/30 px-4 py-2">
        <summary className="cursor-pointer text-[13px] text-ink-2">
          {t("agents.danger")} · {t("agents.danger_hint")}
        </summary>
        <div className="pt-2 pb-1">
          <FreezeCard compact />
        </div>
      </details>
    </div>
  );
}
