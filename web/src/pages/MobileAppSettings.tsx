import { lazy, Suspense } from "react";
import { PageHeader } from "../components/ui";
import { t } from "../i18n";

// The installed app's settings (install guide with a QR code, notifications, devices): the same
// component the app shows under Víc → Nastavení, loaded on demand.
const MobileSettings = lazy(() => import("../mobile/Settings"));

/** Nastavení → Mobilní aplikace. */
export default function MobileAppSettings() {
  return (
    <div className="flex max-w-3xl flex-col gap-5">
      <PageHeader kicker={t("settings.kicker")} title={t("nav.mobile")} sub={t("settings.blurb.mobile")} />
      <Suspense fallback={<p className="text-sm text-ink-2">{t("act.loading")}</p>}>
        <MobileSettings />
      </Suspense>
    </div>
  );
}
