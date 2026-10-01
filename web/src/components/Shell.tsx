import { ChevronDown, LogOut, MoreHorizontal, Power, X } from "lucide-react";
import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { type FreezeState, agentsApi } from "../agentsApi";
import { label, t } from "../i18n";
import { useSubsystems } from "../knowledgeApi";
import { useLive } from "../liveStream";
import { useNeedsMe } from "../needsMeApi";
import { KNOWLEDGE_TABS, SECTIONS, SETTINGS, SETTINGS_ROOT, type SubSection, sectionOf } from "../sections";
import { OverlayHost } from "./overlay";

// The task panel (and the Markdown it renders) loads the first time a task or approval is opened.
const TaskSheetHost = lazy(() => import("./tasks/TaskSheet"));

function TaskSheetSlot() {
  const loc = useLocation();
  const q = new URLSearchParams(loc.search);
  const open = /^\/tasks\/T-\d+$/i.test(loc.pathname) || q.has("task") || q.has("approval");
  if (!open) return null;
  return (
    <Suspense fallback={null}>
      <TaskSheetHost />
    </Suspense>
  );
}

export function Mark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#6cc4dc" strokeWidth="1.4" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3" fill="#6cc4dc" />
      <path d="M12 1v4M12 19v4M1 12h4M19 12h4" />
    </svg>
  );
}

/** The kill switch state for the banner (the switch itself lives on Tým and Nastavení). */
export function useFreeze() {
  // Pushed by the live stream (liveStream.ts); polled only while it is down.
  const live = useLive<FreezeState>("freeze", agentsApi.freezeState, 30_000);
  const [local, setLocal] = useState<FreezeState | null>(null);
  useEffect(() => setLocal(null), [live]);
  useEffect(() => {
    // Right after this tab toggles the switch: show the new state at once.
    const load = () => agentsApi.freezeState().then(setLocal, () => undefined);
    window.addEventListener("pos:freeze", load);
    return () => window.removeEventListener("pos:freeze", load);
  }, []);
  return local ?? live ?? { frozen: false };
}

function FrozenBanner() {
  const freeze = useFreeze();
  if (!freeze.frozen) return null;
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-amber-400/70 bg-amber-300/5 px-4 py-2.5">
      <Power size={15} className="text-amber-300" />
      <span className="text-sm text-amber-200">{t("freeze.banner")}</span>
      <span className="text-xs text-ink-2">{freeze.reason || t("freeze.frozen_sub")}</span>
      <button
        className="btn-accent ml-auto"
        onClick={() => agentsApi.unfreeze().then(() => window.dispatchEvent(new Event("pos:freeze")))}
      >
        {t("freeze.unfreeze")}
      </button>
    </div>
  );
}

function Badge({ n, className = "" }: { n: number; className?: string }) {
  if (!n) return null;
  return (
    <span
      className={`rounded-sm bg-amber-300/15 px-1.5 font-mono text-xs text-amber-300 ${className}`}
      aria-label={t("nav.needs_you", { n })}
    >
      {n}
    </span>
  );
}

const railItem = (active: boolean) =>
  `flex h-[38px] items-center gap-3 rounded px-3 text-sm transition ${
    active ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : "text-ink-2 hover:bg-raised hover:text-ink"
  }`;

function SettingsGroup({ current }: { current: string }) {
  const inside = current === "/settings";
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem("pos.nav.settings") === "open";
    } catch {
      return false;
    }
  });
  const shown = open || inside;
  const toggle = () => {
    setOpen(!shown);
    try {
      localStorage.setItem("pos.nav.settings", shown ? "closed" : "open");
    } catch {
      /* private window */
    }
  };
  const Icon = SETTINGS_ROOT.icon;
  return (
    <div className="flex flex-col gap-0.5">
      <button type="button" onClick={toggle} aria-expanded={shown} className={railItem(inside && !shown)}>
        <Icon size={18} strokeWidth={1.5} className={inside ? "text-accent" : "text-ink-2"} />
        {t(SETTINGS_ROOT.key)}
        <ChevronDown size={14} className={`ml-auto text-ink-2 transition ${shown ? "rotate-180" : ""}`} />
      </button>
      {shown &&
        [{ ...SETTINGS_ROOT, key: "settings.overview" } as SubSection, ...SETTINGS].map((s) => (
          <NavLink key={s.path} to={s.path} end className={({ isActive }) => `${railItem(isActive)} h-[34px]! pl-10! text-[13px]`}>
            {t(s.key)}
          </NavLink>
        ))}
    </div>
  );
}

function MoreSheet({ onClose, onLogout }: { onClose: () => void; onLogout?: () => void }) {
  const loc = useLocation();
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  const item = (s: SubSection) => {
    const Icon = s.icon;
    const active = loc.pathname === s.path;
    return (
      <Link
        key={s.path}
        to={s.path}
        onClick={onClose}
        className={`flex h-12 items-center gap-3 rounded px-3 text-[15px] ${active ? "bg-raised text-ink" : "text-ink-2 hover:bg-raised"}`}
      >
        <Icon size={18} strokeWidth={1.5} className={active ? "text-accent" : "text-ink-2"} />
        {t(s.key)}
      </Link>
    );
  };
  return (
    <div className="fixed inset-0 z-40 bg-black/60 lg:hidden" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("nav.more")}
        className="absolute inset-x-0 bottom-0 max-h-[85dvh] overflow-y-auto rounded-t-xl border-t border-line bg-surface px-4 pt-3 pb-8"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-center">
          <span className="text-base font-medium">{t("nav.more")}</span>
          <button aria-label={t("nav.close_more")} onClick={onClose} className="ml-auto p-2 text-ink-2">
            <X size={18} />
          </button>
        </div>
        <p className="px-3 pt-2 pb-1 text-xs font-medium text-ink-2">{t("nav.knowledge")}</p>
        {[{ ...KNOWLEDGE_TABS[0], key: "nav.knowledge" }, ...KNOWLEDGE_TABS.slice(1)].map(item)}
        <p className="px-3 pt-4 pb-1 text-xs font-medium text-ink-2">{t("nav.settings")}</p>
        {[{ ...SETTINGS_ROOT, key: "settings.overview" } as SubSection, ...SETTINGS].map(item)}
        {onLogout && (
          <button
            type="button"
            onClick={onLogout}
            className="mt-4 flex h-12 w-full items-center gap-3 rounded border border-line px-3 text-[15px] text-ink-2"
          >
            <LogOut size={18} strokeWidth={1.5} /> {t("nav.logout")}
          </button>
        )}
      </div>
    </div>
  );
}

export default function Shell({ children, onLogout }: { children: ReactNode; onLogout?: () => void }) {
  const subsystems = useSubsystems();
  const needs = useNeedsMe();
  const loc = useLocation();
  const current = sectionOf(loc.pathname);
  const [more, setMore] = useState(false);
  const count = needs?.count ?? 0;
  const inMore = current === "/settings" || current === "/knowledge";

  return (
    <div className="min-h-screen overflow-x-clip bg-bg">
      {/* Desktop rail */}
      <aside className="fixed inset-y-0 left-0 hidden w-52 flex-col gap-6 overflow-y-auto border-r border-line px-3.5 pt-6 pb-5 lg:flex">
        <div className="flex items-center gap-2.5 px-2">
          <Mark />
          <span className="font-medium tracking-[-0.01em]">PersonalOS</span>
        </div>
        <nav aria-label={t("nav.main")} className="flex flex-col gap-0.5">
          {SECTIONS.map(({ path, key, icon: Icon }) => {
            const active = current === path;
            return (
              <Link key={path} to={path} aria-current={active ? "page" : undefined} className={railItem(active)}>
                <Icon size={18} strokeWidth={1.5} className={active ? "text-accent" : "text-ink-2"} />
                {t(key)}
                {path === "/today" && <Badge n={count} className="ml-auto" />}
              </Link>
            );
          })}
          <SettingsGroup current={current} />
        </nav>
        <div className="mt-auto flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="flex items-center justify-between text-xs text-ink-2">
            {t("nav.subsystems")} <span>{t("nav.live")}</span>
          </span>
          {subsystems?.map((s) => (
            <span key={s.name} className="flex items-center gap-2 text-[13px] text-ink-2" title={s.detail}>
              <span className={`h-1.5 w-1.5 rounded-full ${s.ok ? "bg-accent" : "bg-ink-3"}`} />
              {label("subsys", s.name)}
              <span className="ml-auto font-mono text-xs">
                {s.value !== null ? `${s.value}${s.unit}` : s.ok ? s.detail.split(" ")[0] : t("nav.off")}
              </span>
            </span>
          ))}
        </div>
        {onLogout && (
          <button
            type="button"
            onClick={onLogout}
            className="flex items-center gap-3 rounded px-3 py-2 text-sm text-ink-2 hover:bg-raised hover:text-ink"
          >
            <LogOut size={16} strokeWidth={1.5} /> {t("nav.logout")}
          </button>
        )}
      </aside>

      {/* Phone top bar */}
      <header className="sticky top-0 z-20 flex items-center gap-2.5 border-b border-line bg-bg/90 px-4 py-3 backdrop-blur lg:hidden">
        <Mark size={20} />
        <span className="font-medium">PersonalOS</span>
        {count > 0 && (
          <Link to="/today" className="ml-auto text-xs text-amber-300">
            {t("nav.needs_you", { n: count })}
          </Link>
        )}
      </header>

      <main className="min-w-0 px-4 pt-5 pb-24 sm:px-6 lg:ml-52 lg:px-9 lg:pt-6 lg:pb-6">
        <FrozenBanner />
        {children}
      </main>

      {/* Phone tab bar: Domů · Chat · Práce · Tým · Víc */}
      <nav aria-label={t("nav.main")} className="fixed inset-x-0 bottom-0 z-20 flex border-t border-line bg-bg px-1 pt-1.5 pb-3 lg:hidden">
        {SECTIONS.filter((s) => s.mobile).map(({ path, key, icon: Icon }) => {
          const active = current === path && !more;
          return (
            <Link
              key={path}
              to={path}
              aria-current={active ? "page" : undefined}
              className={`flex min-w-0 flex-1 flex-col items-center gap-1 py-1.5 text-xs ${active ? "text-accent" : "text-ink-2"}`}
            >
              <span className="relative">
                <Icon size={20} strokeWidth={1.5} />
                {path === "/today" && count > 0 && (
                  <span className="absolute -top-1.5 -right-3 min-w-4 rounded-full bg-amber-300 px-1 text-center font-mono text-[12px] leading-4 text-bg">
                    {count}
                  </span>
                )}
              </span>
              {t(key)}
            </Link>
          );
        })}
        <button
          type="button"
          onClick={() => setMore(true)}
          aria-haspopup="dialog"
          className={`flex min-w-0 flex-1 flex-col items-center gap-1 py-1.5 text-xs ${more || inMore ? "text-accent" : "text-ink-2"}`}
        >
          <MoreHorizontal size={20} strokeWidth={1.5} />
          {t("nav.more")}
        </button>
      </nav>
      {more && <MoreSheet onClose={() => setMore(false)} onLogout={onLogout} />}
      <TaskSheetSlot />
      <OverlayHost />
    </div>
  );
}
