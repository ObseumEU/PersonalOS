import { Check } from "lucide-react";
import { t } from "../i18n/core";
import type { NeedsItem } from "../needsMeApi";

/** A decision card's options (ask_owner with options): one button per option, the recommended one
 * marked, and when the recommendation applies by itself. Small: it ships in the /m bundle too. */
export default function DecisionOptions({ it, busy, onChoose, big }: { it: NeedsItem; busy: boolean; onChoose: (option: string) => void; big?: boolean }) {
  if (!it.options?.length) return null;
  const when = it.default_at ? new Date(it.default_at) : null;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-2">
        {it.options.map((o) => {
          const rec = o === it.recommendation;
          return (
            <button
              key={o}
              disabled={busy}
              onClick={() => onChoose(o)}
              className={`${rec ? "btn-accent" : "btn"} ${big ? "min-h-11 px-3" : ""} max-w-full text-left whitespace-normal`}
              title={rec ? t("needs.card.recommended") : undefined}
            >
              {rec && <Check size={14} aria-hidden />}
              <span className="break-words">{o}</span>
            </button>
          );
        })}
      </div>
      <span className="text-xs text-ink-2">
        {it.recommendation ? t("needs.card.recommendation", { option: it.recommendation }) : ""}
        {when ? ` · ${t("needs.card.default", { when: when.toLocaleString("cs-CZ", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" }) })}` : ""}
      </span>
    </div>
  );
}
