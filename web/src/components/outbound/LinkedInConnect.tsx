import { Link2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../../api";
import { t } from "../../i18n";
import type { LinkedInStatus } from "../../outboundApi";

/** "Připojit LinkedIn" and the connection's state. The button is a plain link: the server redirects to
 * LinkedIn's consent and back. Without `status` it loads its own (owner only). */
export default function LinkedInConnect({ status, className = "" }: { status?: LinkedInStatus; className?: string }) {
  const [s, setS] = useState<LinkedInStatus | null>(status ?? null);
  useEffect(() => {
    if (status) setS(status);
    else api<LinkedInStatus>("/api/integrations/linkedin/status").then(setS, () => undefined);
  }, [status]);
  if (!s) return null;
  const until = s.expires_at ? new Date(s.expires_at * 1000).toLocaleDateString("cs-CZ") : null;
  const state = s.connected
    ? t("linkedin.connected", { name: s.name ?? "?" }) + (until ? ` · ${t("linkedin.until", { date: until })}` : "")
    : s.app
      ? t("linkedin.not_connected")
      : t("linkedin.no_app");
  return (
    <div className={`flex flex-wrap items-center gap-3 ${className}`}>
      <span className="flex min-w-0 flex-1 items-center gap-2 text-[13px]">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.connected ? "bg-accent" : "bg-dim"}`} />
        <span className="shrink-0 font-medium">LinkedIn</span>
        <span className="min-w-0 break-words text-xs text-ink-2">{state}</span>
      </span>
      {s.app ? (
        <a href="/api/integrations/linkedin/start" className={s.connected ? "btn" : "btn-accent"}>
          <Link2 size={14} /> {t(s.connected ? "linkedin.reconnect" : "linkedin.connect")}
        </a>
      ) : (
        <button className="btn" disabled title={t("linkedin.no_app")}>
          <Link2 size={14} /> {t("linkedin.connect")}
        </button>
      )}
    </div>
  );
}
