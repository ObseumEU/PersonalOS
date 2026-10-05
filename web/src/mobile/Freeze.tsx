import { Power } from "lucide-react";
import { useEffect, useState } from "react";
import type { FreezeState } from "../agentsApi";
import { api } from "../api";
import { confirmDialog, toast } from "../components/overlay";
import { t } from "../i18n/core";
import { errText } from "./ui";

/*
 * The kill switch on the phone: the banner while every agent is frozen, and the switch itself
 * (Víc → Nouzové zastavení). The state comes from the tab's live stream (imported on demand, so the
 * first load stays small) and is polled only while the stream is down. Both directions ask first.
 */

let state: FreezeState | null = null;
const subs = new Set<(s: FreezeState) => void>();
function set(s: FreezeState) {
  state = s;
  subs.forEach((f) => f(s));
}
const load = () => api<FreezeState>("/api/system/freeze").then(set, () => undefined);

let streamed = false;
let streamDown = false;
function fromStream() {
  if (streamed) return;
  streamed = true;
  void import("../liveStream").then((live) => {
    live.subscribe<FreezeState>("freeze", set);
    live.onStatus((st) => (streamDown = st === "down"));
  });
}

export function useFreeze(): FreezeState | null {
  const [s, setS] = useState<FreezeState | null>(state);
  useEffect(() => {
    fromStream();
    subs.add(setS);
    if (state) setS(state);
    const first = state ? undefined : window.setTimeout(() => !state && load(), 2500);
    const poll = setInterval(() => streamDown && document.visibilityState === "visible" && load(), 30000);
    return () => {
      subs.delete(setS);
      window.clearTimeout(first);
      clearInterval(poll);
    };
  }, []);
  return s;
}

/** Freeze (with a reason) or unfreeze every agent, after a confirmation. False: cancelled or failed. */
export async function toggleFreeze(frozen: boolean): Promise<boolean> {
  const answer = frozen
    ? await confirmDialog({ title: t("m.freeze.unfreeze_confirm"), body: t("m.freeze.unfreeze_body"), confirm: t("freeze.unfreeze") })
    : await confirmDialog({ title: t("freeze.confirm_title"), body: t("freeze.confirm_body"), confirm: t("freeze.freeze"), danger: true, reason: t("freeze.reason") });
  if (answer === null) return false;
  try {
    set(await api<FreezeState>(frozen ? "/api/system/unfreeze" : "/api/system/freeze", { method: "POST", body: JSON.stringify(frozen ? {} : { reason: answer }) }));
    toast(frozen ? t("m.freeze.unfrozen") : t("m.freeze.frozen"));
    window.dispatchEvent(new Event("pos:freeze"));
    return true;
  } catch (e) {
    toast(errText(e), { error: true });
    return false;
  }
}

/** Over the lists while the agents are frozen: why, and "Rozmrazit". */
export function FrozenBanner() {
  const s = useFreeze();
  if (!s?.frozen) return null;
  return (
    <div role="status" className="flex items-center gap-3 border-b border-amber-400/60 bg-amber-300/10 px-4 py-2.5">
      <Power size={18} className="shrink-0 text-amber-300" />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-[14px] text-amber-200">{t("freeze.banner")}</span>
        <span className="truncate text-[12px] text-ink-2">{s.reason || t("freeze.frozen_sub")}</span>
      </span>
      <button onClick={() => toggleFreeze(true)} className="h-10 shrink-0 rounded-lg border border-accent bg-accent/10 px-3 text-[14px] text-accent">
        {t("freeze.unfreeze")}
      </button>
    </div>
  );
}

/** The switch as a row in Víc. */
export function FreezeRow() {
  const s = useFreeze();
  const frozen = !!s?.frozen;
  return (
    <button onClick={() => s && toggleFreeze(frozen)} disabled={!s} className="flex min-h-14 items-center gap-3 border-b border-line px-4 text-left text-[16px] active:bg-raised disabled:opacity-50">
      <Power size={20} className={frozen ? "text-amber-300" : "text-ink-2"} />
      <span className="flex flex-col">
        {frozen ? t("freeze.unfreeze") : t("freeze.freeze")}
        <span className="text-[13px] text-ink-2">{frozen ? s?.reason || t("freeze.frozen_sub") : t("freeze.idle_sub")}</span>
      </span>
    </button>
  );
}
