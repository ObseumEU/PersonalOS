import { useEffect, useState } from "react";
import { t } from "../../i18n";
import { type DraftTrust, outboundApi } from "../../outboundApi";
import { Panel } from "../ui";

/** "Důvěra v koncepty": how the owner treats the agents' e-mail drafts (sent unchanged / edited / discarded). */
export default function DraftTrustPanel({ className = "" }: { className?: string }) {
  const [trust, setTrust] = useState<DraftTrust | null>(null);
  useEffect(() => {
    outboundApi.drafts().then((d) => setTrust(d.trust), () => undefined);
  }, []);
  if (!trust) return null;
  const cells: [string, number][] = [
    ["outbound.trust.unchanged", trust.sent_unchanged],
    ["outbound.trust.edited", trust.sent_edited],
    ["outbound.trust.discarded", trust.discarded],
    ["outbound.trust.waiting", trust.waiting],
  ];
  return (
    <Panel title={t("outbound.trust.title")} right={trust.unchanged_rate == null ? "–" : `${Math.round(trust.unchanged_rate * 100)} %`} className={className}>
      <div className="flex flex-col gap-3 p-3">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {cells.map(([k, v]) => (
            <div key={k} className="rounded border border-line bg-bg p-2">
              <div className="text-lg tabular-nums">{v}</div>
              <div className="text-xs text-ink-2">{t(k)}</div>
            </div>
          ))}
        </div>
        <p className={`text-xs ${trust.ready_for_auto ? "text-accent" : "text-ink-2"}`}>{trust.advice}</p>
      </div>
    </Panel>
  );
}
