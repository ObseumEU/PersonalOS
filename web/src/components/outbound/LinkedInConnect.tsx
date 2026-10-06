import { Link2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../../api";
import { t } from "../../i18n";
import { ownerActions } from "../../needsMeApi";
import type { LinkedInStatus } from "../../outboundApi";
import { toast } from "../overlay";

/** "Připojit LinkedIn" and the connection's state. The button never asks for keys: an agent prepares the LinkedIn
 * app and the consent in its own browser and hands the owner only the login and "Allow" ("Čeká na tebe").
 * Without `status` it loads its own (owner only). */
export default function LinkedInConnect({ status, className = "" }: { status?: LinkedInStatus; className?: string }) {
  const [s, setS] = useState<LinkedInStatus | null>(status ?? null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (status) setS(status);
    else api<LinkedInStatus>("/api/integrations/linkedin/status").then(setS, () => undefined);
  }, [status]);
  if (!s) return null;
  const until = s.expires_at ? new Date(s.expires_at * 1000).toLocaleDateString("cs-CZ") : null;
  const state = s.connected
    ? t("linkedin.connected", { name: s.name ?? "?" }) + (until ? ` · ${t("linkedin.until", { date: until })}` : "")
    : s.flow
      ? t("linkedin.in_progress", { agent: s.flow.agent ?? "agent", ref: s.flow.task_ref })
      : t("linkedin.not_connected");
  const connect = async () => {
    setBusy(true);
    try {
      const r = await ownerActions.connectLinkedIn();
      setS({ ...s, flow: { task_ref: r.task_ref, agent: r.agent, status: "next" } });
      toast(t("needs.done.linkedin_agent"));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`flex flex-wrap items-center gap-3 ${className}`}>
      <span className="flex min-w-0 flex-1 items-center gap-2 text-[13px]">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.connected ? "bg-accent" : "bg-dim"}`} />
        <span className="shrink-0 font-medium">LinkedIn</span>
        <span className="min-w-0 break-words text-xs text-ink-2">{state}</span>
      </span>
      {s.connected && s.app ? (
        <a href="/api/integrations/linkedin/start" className="btn">
          <Link2 size={14} /> {t("linkedin.reconnect")}
        </a>
      ) : (
        <button className="btn-accent" disabled={busy || !!s.flow} onClick={connect} title={t("linkedin.agent_hint")}>
          <Link2 size={14} /> {t("linkedin.connect")}
        </button>
      )}
    </div>
  );
}
