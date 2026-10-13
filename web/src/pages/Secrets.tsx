import { Link } from "react-router-dom";
import SecretsView from "../components/SecretsView";
import { PageHeader } from "../components/ui";
import { t } from "../i18n";

/** Nastavení → Tajemství: the agents' secrets (pos.credentials.secrets). Owner only; never a value. */
export default function Secrets() {
  return (
    <div className="flex min-w-0 flex-col gap-5 pb-20">
      <PageHeader kicker={t("settings.kicker")} title={t("nav.secrets")} sub={t("sec.sub")} />
      <Link to="/credentials" className="self-start text-xs text-ink-2 hover:text-accent">
        {t("nav.credentials")} →
      </Link>
      <SecretsView />
    </div>
  );
}
