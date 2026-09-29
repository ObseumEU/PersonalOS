import { Eye, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../../api";
import type { TraceEntry } from "../../agentsApi";
import { ago, fmtTime, t } from "../../i18n";
import { confirmDialog, toast } from "../overlay";
import { Panel } from "../ui";

type Live = { run_id: number; running: boolean; frame: boolean; version?: number; url?: string | null; kind?: string; step?: string; at?: string };

/** The agent's browser (or desktop) as it is now: while the run page is open the guard sends a
 * frame every few seconds (pos.browser live view); nothing shows for a run that does not browse. */
export function BrowserLive({ runId }: { runId: number | null }) {
  const [live, setLive] = useState<Live | null>(null);
  useEffect(() => {
    setLive(null);
    if (runId == null) return;
    let stop = false;
    const tick = () =>
      api<Live>(`/api/runs/${runId}/live`).then(
        (l) => !stop && setLive(l),
        () => undefined,
      );
    tick();
    const timer = window.setInterval(tick, 2500);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [runId]);
  if (!live?.frame) return null;
  return (
    <Panel
      title={t(live.kind === "computer" ? "agent.live_desktop" : "agent.live")}
      right={t("agent.live_right", { id: live.run_id })}
      className="min-w-0 lg:col-span-12"
    >
      <div className="flex flex-col gap-1.5 px-4 py-3">
        <a href={`/api/runs/${live.run_id}/live.img?v=${live.version}`} target="_blank" rel="noreferrer">
          <img
            src={`/api/runs/${live.run_id}/live.img?v=${live.version}`}
            alt={live.url ?? ""}
            className="max-h-[420px] w-full rounded border border-line object-contain object-left-top bg-black/20"
          />
        </a>
        <span className="flex min-w-0 items-center gap-2 text-xs text-ink-2">
          {live.running && <Eye size={12} className="shrink-0 text-accent" />}
          {live.step && <span className="shrink-0 text-accent">{live.step}</span>}
          <span className="truncate font-mono">{live.url}</span>
          {live.at && <span className="ml-auto shrink-0">{t("agent.live_stale", { ago: ago(live.at) })}</span>}
        </span>
      </div>
    </Panel>
  );
}

/** The browser steps of the trace as a strip of thumbnails (each links to its full screenshot). */
export function Filmstrip({ trace }: { trace: TraceEntry[] }) {
  const shots = trace.filter((e) => typeof e.detail.screenshot === "string" && /^(browser|computer):/.test(e.action));
  if (shots.length === 0) return null;
  return (
    <div className="border-b border-line px-4 py-2">
      <span className="text-xs text-ink-2">{t("agent.filmstrip")}</span>
      <div className="mt-1 flex gap-2 overflow-x-auto pb-1">
        {shots.map((e) => {
          const src = `/api/browser/screenshots/${e.detail.screenshot as string}`;
          const url = typeof e.detail.url === "string" ? e.detail.url : "";
          return (
            <a key={e.id} href={src} target="_blank" rel="noreferrer" title={`${e.action} · ${url}`} className="shrink-0">
              <img src={src} alt={e.action} loading="lazy" className={`h-16 w-28 rounded border object-cover object-left-top ${e.detail.ok === false ? "border-red-400" : "border-line"}`} />
              <span className="block w-28 truncate text-[10px] text-ink-2">
                {fmtTime(e.at)} {e.action.split(":")[1]}
              </span>
            </a>
          );
        })}
      </div>
    </div>
  );
}

type Profile = { granted: boolean; exists: boolean; sites?: string[]; updated_at?: string };

/** browser:profile: the agent's kept logins (encrypted by PersonalOS); the owner clears them. */
export function BrowserProfilePanel({ agentId }: { agentId: number }) {
  const [p, setP] = useState<Profile | null>(null);
  const load = () => api<Profile>(`/api/agents/${agentId}/browser-profile`).then(setP, () => setP(null));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);
  if (!p || (!p.granted && !p.exists)) return null;
  const clear = async () => {
    const ok = await confirmDialog({ title: t("agent.browser_profile_confirm"), body: t("agent.browser_profile_body"), confirm: t("act.confirm"), danger: true });
    if (ok === null) return;
    api(`/api/agents/${agentId}/browser-profile`, { method: "DELETE" }).then(
      () => {
        toast(t("agent.browser_profile_cleared"));
        load();
      },
      (e) => toast(e.message, { error: true }),
    );
  };
  return (
    <Panel title={t("agent.browser_profile")} className="min-w-0 lg:col-span-12">
      <div className="flex flex-wrap items-center gap-3 px-4 py-2 text-xs text-ink-2">
        {p.exists ? (
          <>
            <span>{t("agent.browser_profile_sites", { sites: (p.sites ?? []).join(", ") || "—" })}</span>
            {p.updated_at && <span>{ago(p.updated_at)}</span>}
            <button className="ml-auto inline-flex items-center gap-1 text-ink-2 hover:text-red-400" onClick={clear}>
              <Trash2 size={13} /> {t("agent.browser_profile_clear")}
            </button>
          </>
        ) : (
          <span>{t("agent.browser_profile_none")}</span>
        )}
      </div>
    </Panel>
  );
}
