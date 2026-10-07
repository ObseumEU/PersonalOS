import { Bell, BellOff, Download, LogOut, Monitor, Send, Smartphone } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { confirmDialog, toast } from "../components/overlay";
import { ago, t } from "../i18n/core";
import {
  type PushConfig,
  type PushPrefs,
  type PushTestResult,
  currentSubscription,
  disablePush,
  enablePush,
  isAndroid,
  isIOS,
  isStandalone,
  promptInstall,
  pushApi,
  pushSupported,
  syncPush,
  useInstallable,
} from "./pwa";
import { LoadError, errText } from "./ui";

type Device = { id: string; label: string; app: boolean; created_at: string; last_seen_at: string; current: boolean; push: boolean };

export const appUrl = () => `${window.location.origin}/m`;

/** A QR code of the app's address (the generator is loaded only here). */
function Qr({ text }: { text: string }) {
  const [svg, setSvg] = useState<string | null>(null);
  useEffect(() => {
    import("qrcode-generator").then(({ default: qrcode }) => {
      const q = qrcode(0, "M");
      q.addData(text);
      q.make();
      setSvg(q.createSvgTag({ cellSize: 4, margin: 2, scalable: true }));
    });
  }, [text]);
  return (
    <div
      role="img"
      aria-label={text}
      className="h-44 w-44 shrink-0 rounded-lg bg-white p-1 [&>svg]:h-full [&>svg]:w-full"
      // qrcode-generator's own SVG (only the given URL is encoded)
      dangerouslySetInnerHTML={svg ? { __html: svg } : undefined}
    />
  );
}

function Section({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <section className="panel flex flex-col gap-3 p-4">
      <h2 className="flex items-center gap-2 text-[15px] font-medium">
        <span className="text-accent">{icon}</span>
        {title}
      </h2>
      {children}
    </section>
  );
}

function Toggle({ label, hint, on, onChange, disabled }: { label: string; hint?: string; on: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={`flex min-h-12 items-center gap-3 ${disabled ? "opacity-50" : ""}`}>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="text-[15px]">{label}</span>
        {hint && <span className="text-[12px] leading-snug text-ink-2">{hint}</span>}
      </span>
      <input type="checkbox" role="switch" className="peer sr-only" checked={on} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span
        aria-hidden
        className="relative h-7 w-12 shrink-0 rounded-full border border-line bg-raised transition peer-checked:border-accent peer-checked:bg-accent/30 peer-focus-visible:ring-2 peer-focus-visible:ring-accent after:absolute after:top-0.5 after:left-0.5 after:h-5.5 after:w-5.5 after:rounded-full after:bg-ink-2 after:transition peer-checked:after:translate-x-5 peer-checked:after:bg-accent"
      />
    </label>
  );
}

export function InstallSection() {
  const can = useInstallable();
  const url = appUrl();
  return (
    <Section title={t("m.install.title")} icon={<Download size={18} />}>
      {isStandalone() ? (
        <p className="text-sm text-ink-2">{t("m.install.installed")}</p>
      ) : (
        can && (
          <button className="btn-accent h-11! justify-center text-[15px]!" onClick={() => promptInstall()}>
            <Download size={16} /> {t("m.install.button")}
          </button>
        )
      )}
      <div className="flex flex-col gap-4 sm:flex-row">
        <div className="flex flex-col items-center gap-1.5">
          <Qr text={url} />
          <span className="max-w-44 text-center text-[12px] text-ink-2">{t("m.install.qr")}</span>
        </div>
        <div className="flex min-w-0 flex-col gap-3 text-[14px] leading-relaxed">
          <div>
            <h3 className="flex items-center gap-1.5 font-medium"><Smartphone size={15} /> {t("m.install.android")}</h3>
            <ol className="list-decimal pl-5 text-ink-2">
              <li>{t("m.install.android.1", { url })}</li>
              <li>{t("m.install.android.2")}</li>
              <li>{t("m.install.android.3")}</li>
            </ol>
          </div>
          <div>
            <h3 className="flex items-center gap-1.5 font-medium"><Smartphone size={15} /> {t("m.install.ios")}</h3>
            <ol className="list-decimal pl-5 text-ink-2">
              <li>{t("m.install.ios.1", { url })}</li>
              <li>{t("m.install.ios.2")}</li>
              <li>{t("m.install.ios.3")}</li>
            </ol>
          </div>
          <div>
            <h3 className="flex items-center gap-1.5 font-medium"><Monitor size={15} /> {t("m.install.pc")}</h3>
            <ol className="list-decimal pl-5 text-ink-2">
              <li>{t("m.install.pc.1", { url })}</li>
              <li>{t("m.install.pc.2")}</li>
            </ol>
          </div>
          <p className="text-[13px] text-ink-2">{t("m.install.updates")}</p>
          <p className="text-[13px] text-ink-2">{t("m.install.vpn")}</p>
        </div>
      </div>
    </Section>
  );
}

export function PushSection() {
  const [cfg, setCfg] = useState<PushConfig | null>(null);
  const [subscribed, setSubscribed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [results, setResults] = useState<PushTestResult[] | null>(null);
  const supported = pushSupported();
  const permission = supported ? Notification.permission : "denied";
  const ios = isIOS();
  const load = useCallback(() => {
    pushApi.config().then(
      async (c) => {
        setCfg(c);
        setFailed(null);
        // The browser has a subscription the server does not know (or may make one): register it again.
        if (await syncPush(c)) setCfg(await pushApi.config());
        currentSubscription().then((s) => setSubscribed(!!s), () => undefined);
      },
      (e) => setFailed(errText(e)),
    );
  }, []);
  useEffect(load, [load]);

  const save = (changes: Partial<PushPrefs>) => {
    if (!cfg) return;
    setCfg({ ...cfg, prefs: { ...cfg.prefs, ...changes } });
    pushApi.prefs(changes).then((prefs) => setCfg((c) => (c ? { ...c, prefs } : c)), (e) => toast(e.message, { error: true }));
  };
  const wrap = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
      load();
    }
  };
  const testAll = () =>
    wrap(async () => {
      const r = await pushApi.testAll();
      setResults(r.results);
      toast(r.devices === 0 ? t("m.push.test_no_devices") : t("m.push.test_result", { ok: r.sent, n: r.devices }), { error: r.sent < r.devices });
    });
  const p = cfg?.prefs;
  const status = !supported
    ? ios
      ? t("m.push.ios_install")
      : t("m.push.unsupported")
    : cfg && !cfg.enabled
      ? t("m.push.server_off")
      : permission === "denied"
        ? isAndroid()
          ? t("m.push.blocked_android")
          : t("m.push.blocked")
        : subscribed
          ? cfg?.this_device === false
            ? t("m.push.on_unsynced")
            : t("m.push.on")
          : ios && !isStandalone()
            ? t("m.push.ios_install")
            : t("m.push.off");

  return (
    <Section title={t("m.push.title")} icon={<Bell size={18} />}>
      {!cfg && failed ? (
        <LoadError error={failed} onRetry={load} className="py-3!" />
      ) : (
        <p className="text-sm text-ink-2">
          {status}
          {cfg ? ` · ${t("m.push.devices", { n: cfg.devices })}` : ""}
        </p>
      )}
      {supported && cfg?.enabled && permission !== "denied" && (
        <div className="flex flex-wrap gap-2">
          {subscribed ? (
            <button className="btn h-11! text-[15px]!" disabled={busy} onClick={() => wrap(disablePush)}>
              <BellOff size={16} /> {t("m.push.disable")}
            </button>
          ) : (
            <button className="btn-accent h-11! text-[15px]!" disabled={busy} onClick={() => wrap(() => enablePush(cfg.public_key!))}>
              <Bell size={16} /> {t("m.push.enable")}
            </button>
          )}
        </div>
      )}
      {cfg?.enabled && (
        <div className="flex flex-col gap-2">
          <button className="btn h-11! self-start text-[15px]!" disabled={busy || cfg.devices === 0} onClick={testAll}>
            <Send size={16} /> {t("m.push.test_all")}
          </button>
          {cfg.devices === 0 && <p className="text-[12px] text-ink-2">{t("m.push.test_needs_device")}</p>}
          {results && (
            <ul className="flex flex-col gap-1 text-[13px]" aria-live="polite">
              {results.map((r) => (
                <li key={r.sub_id} className="flex flex-wrap items-baseline gap-x-2">
                  <span className={r.ok ? "text-accent" : "text-red-400"}>{r.ok ? "✓" : "✗"}</span>
                  <span>{r.label}</span>
                  <span className="text-ink-2">
                    {r.ok ? t("m.push.test_ok") : r.removed ? t("m.push.test_removed") : r.error || t("m.push.test_failed")}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {p && (
        <div className="flex flex-col divide-y divide-line">
          <Toggle label={t("m.push.cat.chat")} hint={t("m.push.cat.chat_hint")} on={p.chat} onChange={(v) => save({ chat: v })} />
          <Toggle label={t("m.push.cat.needs")} hint={t("m.push.cat.needs_hint")} on={p.needs} onChange={(v) => save({ needs: v })} />
          <Toggle label={t("m.push.cat.urgent")} hint={t("m.push.cat.urgent_hint")} on={p.urgent} onChange={(v) => save({ urgent: v })} />
          <Toggle label={t("m.push.preview")} on={p.preview} onChange={(v) => save({ preview: v })} />
          <Toggle label={t("m.push.quiet")} hint={t("m.push.quiet_hint")} on={p.quiet} onChange={(v) => save({ quiet: v })} />
          {p.quiet && (
            <div className="flex items-center gap-2 py-3 text-[15px]">
              <label className="flex items-center gap-2">
                {t("m.push.from")}
                <input type="time" value={p.quiet_from} onChange={(e) => e.target.value && save({ quiet_from: e.target.value })} className="h-11 rounded-lg border border-line bg-bg px-2 text-[16px]" />
              </label>
              <label className="flex items-center gap-2">
                {t("m.push.to")}
                <input type="time" value={p.quiet_to} onChange={(e) => e.target.value && save({ quiet_to: e.target.value })} className="h-11 rounded-lg border border-line bg-bg px-2 text-[16px]" />
              </label>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

export function DevicesSection() {
  const [list, setList] = useState<Device[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const load = useCallback(
    () =>
      api<Device[]>("/api/auth/devices").then(
        (l) => {
          setList(l);
          setFailed(null);
        },
        (e) => setFailed(errText(e)),
      ),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);
  const revoke = async (d: Device) => {
    const ok = await confirmDialog({ title: t("m.devices.revoke_confirm", { name: d.label }), body: t("m.devices.revoke_body"), confirm: t("m.devices.revoke"), danger: true });
    if (ok === null) return;
    try {
      await api(`/api/auth/devices/${d.id}/revoke`, { method: "POST" });
      toast(t("m.devices.revoked"));
      if (d.current) window.location.reload();
      else load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };
  const revokeMany = async (label: string | null, n: number) => {
    const ok = await confirmDialog({
      title: label ? t("m.devices.revoke_group_confirm", { name: label, n }) : t("m.devices.revoke_others_confirm", { n }),
      body: t("m.devices.revoke_body"),
      confirm: t("m.devices.revoke"),
      danger: true,
    });
    if (ok === null) return;
    try {
      const q = label ? `?label=${encodeURIComponent(label)}` : "";
      const r = await api<{ revoked: number }>(`/api/auth/devices/revoke-others${q}`, { method: "POST" });
      toast(t("m.devices.revoked_n", { n: r.revoked }));
      load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };
  // Devices with the same label that are not this one fold into one row ("Neznámé zařízení ×234").
  const groups: { label: string; items: Device[] }[] = [];
  for (const d of list ?? []) {
    const g = !d.current && groups.find((x) => x.label === d.label && !x.items[0].current);
    if (g) g.items.push(d);
    else groups.push({ label: d.label, items: [d] });
  }
  const others = (list ?? []).filter((d) => !d.current).length;
  return (
    <Section title={t("m.devices.title")} icon={<Smartphone size={18} />}>
      {!list && failed && <LoadError error={failed} onRetry={load} className="py-3!" />}
      {list?.length === 0 && <p className="text-sm text-ink-2">{t("m.devices.none")}</p>}
      {others > 1 && (
        <button className="btn h-10! self-start" onClick={() => revokeMany(null, others)}>
          <LogOut size={14} /> {t("m.devices.revoke_others", { n: others })}
        </button>
      )}
      <ul className="flex flex-col divide-y divide-line">
        {groups.map(({ label: lbl, items }) => {
          const d = items[0];
          const many = items.length > 1;
          return (
            <li key={d.id} className="flex min-h-14 flex-wrap items-center gap-3 py-2">
              {/Android|iPhone|iPad/.test(d.label) ? <Smartphone size={20} className="shrink-0 text-ink-2" /> : <Monitor size={20} className="shrink-0 text-ink-2" />}
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="text-[15px] break-words">
                  {lbl}
                  {many && <span className="ml-1 text-ink-2">×{items.length}</span>}
                  {d.current && <span className="ml-2 text-[12px] text-accent">{t("m.devices.current")}</span>}
                </span>
                <span className="text-[12px] text-ink-2">
                  {[!many && d.app && t("m.devices.app"), items.some((x) => x.push) && t("m.devices.push"), t("m.devices.seen", { when: ago(d.last_seen_at) })]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </span>
              <button className="btn h-10!" onClick={() => (many ? revokeMany(lbl, items.length) : revoke(d))}>
                <LogOut size={14} /> {many ? t("m.devices.revoke_all", { n: items.length }) : t("m.devices.revoke")}
              </button>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

/** Install, notifications and devices: in the app (Víc → Nastavení) and in Nastavení → Mobilní aplikace. */
export default function MobileSettings() {
  return (
    <div className="flex flex-col gap-4">
      <PushSection />
      <InstallSection />
      <DevicesSection />
    </div>
  );
}
