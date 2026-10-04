import { Mail } from "lucide-react";
import { useEffect, useState } from "react";
import { t } from "../../i18n";
import { type EmailPolicy, outboundApi } from "../../outboundApi";
import { confirmDialog, toast } from "../overlay";

/** Nastavení: how agents' e-mail goes out. Draft (the owner sends it) by default; auto after a warning. */
export default function EmailModeCard() {
  const [p, setP] = useState<EmailPolicy | null>(null);
  useEffect(() => {
    outboundApi.policy().then(setP, () => undefined);
  }, []);
  if (!p) return null;
  const auto = p.mode === "auto";
  const canSend = Object.values(p.configured).some((c) => c.send);

  async function toggle() {
    try {
      if (!auto) {
        const ok = await confirmDialog({
          title: t("outbound.auto_confirm_title"),
          body: t(canSend ? "outbound.auto_confirm_body" : "outbound.auto_confirm_no_scope"),
          confirm: t("outbound.auto_on"),
          danger: true,
        });
        if (ok === null) return;
      }
      setP(await outboundApi.setMode(auto ? "draft" : "auto"));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  }

  return (
    <div className={`panel flex flex-wrap items-center gap-3 p-4 ${auto ? "border-amber-400/70" : ""}`}>
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-line text-ink-2">
        <Mail size={16} />
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">{t("outbound.auto_title")}</span>
        <span className="text-xs text-ink-2">{t(auto ? "outbound.auto_sub_on" : "outbound.auto_sub_off")}</span>
      </span>
      <label className="flex shrink-0 cursor-pointer items-center gap-2 text-sm">
        <input type="checkbox" role="switch" checked={auto} onChange={toggle} className="h-4 w-4 accent-amber-400" />
        {t(auto ? "outbound.on" : "outbound.off")}
      </label>
    </div>
  );
}
