import { Power } from "lucide-react";
import { useEffect, useState } from "react";
import { type FreezeState, agentsApi } from "../../agentsApi";

/** The kill switch as a card: one button freezes every agent. */
export default function FreezeCard({ onChange, compact = false }: { onChange?: () => void; compact?: boolean }) {
  const [state, setState] = useState<FreezeState | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    agentsApi.freezeState().then(setState);
  }, []);

  async function toggle() {
    setError(null);
    try {
      if (state?.frozen) setState(await agentsApi.unfreeze());
      else {
        const reason = window.prompt("Freeze every agent now. Why? (optional)");
        if (reason === null) return;
        setState(await agentsApi.freeze(reason));
      }
      window.dispatchEvent(new Event("pos:freeze"));
      onChange?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!state) return null;
  return (
    <div className={`panel flex items-center gap-3 ${compact ? "p-3" : "p-4"} ${state.frozen ? "border-amber-400/70" : ""}`}>
      <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full border ${state.frozen ? "border-amber-300 text-amber-300" : "border-line text-ink-3"}`}>
        <Power size={16} />
      </span>
      <span className="flex min-w-0 flex-col">
        <span className="text-sm font-medium">{state.frozen ? "All agents are frozen" : "Kill switch"}</span>
        <span className="cap truncate">
          {state.frozen ? state.reason || "no new runs, queues stopped" : "one switch stops every agent at once"}
        </span>
        {error && <span className="cap text-red-400!">{error}</span>}
      </span>
      <button type="button" onClick={toggle} className={`ml-auto shrink-0 ${state.frozen ? "btn-accent" : "btn border-amber-400/60! text-amber-300!"}`}>
        {state.frozen ? "Unfreeze" : "Freeze all"}
      </button>
    </div>
  );
}
