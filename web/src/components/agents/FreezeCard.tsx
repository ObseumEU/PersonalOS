import { Power } from "lucide-react";
import { useEffect, useState } from "react";
import { type FreezeState, agentsApi } from "../../agentsApi";
import { t } from "../../i18n";
import { confirmDialog, toast } from "../overlay";

/** The kill switch as a card: one button freezes every agent, after a confirmation. */
export default function FreezeCard({ onChange, compact = false }: { onChange?: () => void; compact?: boolean }) {
  const [state, setState] = useState<FreezeState | null>(null);
  useEffect(() => {
    const load = () => agentsApi.freezeState().then(setState, () => undefined);
    load();
    window.addEventListener("pos:freeze", load);
    return () => window.removeEventListener("pos:freeze", load);
  }, []);

  async function toggle() {
    try {
      if (state?.frozen) setState(await agentsApi.unfreeze());
      else {
        const reason = await confirmDialog({
          title: t("freeze.confirm_title"),
          body: t("freeze.confirm_body"),
          confirm: t("freeze.freeze"),
          danger: true,
          reason: t("freeze.reason"),
        });
        if (reason === null) return;
        setState(await agentsApi.freeze(reason));
      }
      window.dispatchEvent(new Event("pos:freeze"));
      onChange?.();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  }

  if (!state) return null;
  return (
    <div className={`panel flex flex-wrap items-center gap-3 ${compact ? "p-3" : "p-4"} ${state.frozen ? "border-amber-400/70" : ""}`}>
      <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full border ${state.frozen ? "border-amber-300 text-amber-300" : "border-line text-ink-2"}`}>
        <Power size={16} />
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">{state.frozen ? t("freeze.frozen_title") : t("freeze.title")}</span>
        <span className="text-xs text-ink-2">{state.frozen ? state.reason || t("freeze.frozen_sub") : t("freeze.idle_sub")}</span>
      </span>
      <button type="button" onClick={toggle} className={`shrink-0 ${state.frozen ? "btn-accent" : "btn border-amber-400/60! text-amber-300!"}`}>
        {state.frozen ? t("freeze.unfreeze") : t("freeze.freeze")}
      </button>
    </div>
  );
}
