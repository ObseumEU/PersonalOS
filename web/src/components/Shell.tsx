import { LogOut, Power } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { type FreezeState, agentsApi } from "../agentsApi";
import { useSubsystems } from "../knowledgeApi";
import { SECTIONS } from "../sections";
import { MockDot } from "./ui";

export function Mark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#6cc4dc" strokeWidth="1.4" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3" fill="#6cc4dc" />
      <path d="M12 1v4M12 19v4M1 12h4M19 12h4" />
    </svg>
  );
}

/** Live platform state for the chrome: kill switch and approvals waiting. */
function usePlatformState() {
  const [freeze, setFreeze] = useState<FreezeState>({ frozen: false });
  const [approvals, setApprovals] = useState(0);
  useEffect(() => {
    const load = () => {
      agentsApi.freezeState().then(setFreeze, () => undefined);
      agentsApi.approvals().then((a) => setApprovals(a.length), () => undefined);
    };
    load();
    const t = setInterval(load, 20000);
    window.addEventListener("pos:freeze", load);
    window.addEventListener("pos:approvals", load);
    return () => {
      clearInterval(t);
      window.removeEventListener("pos:freeze", load);
      window.removeEventListener("pos:approvals", load);
    };
  }, []);
  return { freeze, setFreeze, approvals };
}

function FrozenBanner({ freeze, onUnfreeze }: { freeze: FreezeState; onUnfreeze: () => void }) {
  if (!freeze.frozen) return null;
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-amber-400/70 bg-amber-300/5 px-4 py-2.5">
      <Power size={15} className="text-amber-300" />
      <span className="text-sm text-amber-200">All agents are frozen.</span>
      <span className="cap">{freeze.reason || "No new runs; queues are stopped. Nothing was deleted."}</span>
      <button className="btn-accent ml-auto" onClick={onUnfreeze}>
        Unfreeze
      </button>
    </div>
  );
}

export default function Shell({ children, onLogout }: { children: ReactNode; onLogout?: () => void }) {
  const subsystems = useSubsystems();
  const { freeze, setFreeze, approvals } = usePlatformState();
  const toggleFreeze = async () => {
    if (freeze.frozen) setFreeze(await agentsApi.unfreeze());
    else {
      const reason = window.prompt("Freeze every agent now. Why? (optional)");
      if (reason === null) return;
      setFreeze(await agentsApi.freeze(reason));
    }
    window.dispatchEvent(new Event("pos:freeze"));
  };
  return (
    <div className="min-h-screen bg-bg">
      {/* Desktop rail */}
      <aside className="fixed inset-y-0 left-0 hidden w-52 flex-col gap-6 border-r border-line px-3.5 pt-6 pb-5 lg:flex">
        <div className="flex items-center gap-2.5 px-2">
          <Mark />
          <span className="font-medium tracking-[-0.01em]">PersonalOS</span>
        </div>
        <nav aria-label="Main" className="flex flex-col gap-0.5">
          {SECTIONS.map(({ path, label, icon: Icon, mock }) => (
            <NavLink
              key={path}
              to={`/${path}`}
              className={({ isActive }) =>
                `flex h-[38px] items-center gap-3 rounded px-3 text-sm transition ${
                  isActive
                    ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]"
                    : "text-ink-2 hover:bg-raised hover:text-ink"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  <Icon size={18} strokeWidth={1.5} className={isActive ? "text-accent" : "text-ink-3"} />
                  {label}
                  {mock && <MockDot className="ml-auto" />}
                  {path === "approvals" && approvals > 0 && (
                    <span className="cap ml-auto rounded-sm bg-amber-300/15 px-1.5 text-amber-300!">{approvals}</span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <span className="cap mt-auto flex items-center gap-2 px-3">
          <MockDot /> = mock, not real yet
        </span>
        <button
          type="button"
          onClick={toggleFreeze}
          title="Kill switch: stop every agent at once"
          className={`flex items-center gap-2.5 rounded-md border px-3 py-2 text-[13px] ${
            freeze.frozen ? "border-amber-400/70 text-amber-300" : "border-line text-ink-2 hover:border-amber-400/60 hover:text-amber-300"
          }`}
        >
          <Power size={15} />
          {freeze.frozen ? "Frozen · unfreeze" : "Freeze all agents"}
        </button>
        <div className="flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="cap flex items-center justify-between">
            SUBSYSTEMS <span className="text-[9px]">live</span>
          </span>
          {subsystems?.map((s) => (
            <span key={s.name} className="flex items-center gap-2 text-[13px] text-ink-2" title={s.detail}>
              <span className={`h-1.5 w-1.5 rounded-full ${s.ok ? "bg-accent" : "bg-ink-3"}`} />
              {s.name.split(" ")[0]}
              <span className="cap ml-auto">{s.value !== null ? `${s.value}${s.unit}` : s.ok ? s.detail.split(" ")[0] : "off"}</span>
            </span>
          ))}
        </div>
        {onLogout && (
          <button
            type="button"
            onClick={onLogout}
            className="flex items-center gap-3 rounded px-3 py-2 text-sm text-ink-3 hover:bg-raised hover:text-ink"
          >
            <LogOut size={16} strokeWidth={1.5} /> Log out
          </button>
        )}
      </aside>

      {/* Phone top bar */}
      <header className="sticky top-0 z-20 flex items-center gap-2.5 border-b border-line bg-bg/90 px-4 py-3 backdrop-blur lg:hidden">
        <Mark size={20} />
        <span className="font-medium">PersonalOS</span>
        <button type="button" onClick={toggleFreeze} aria-label={freeze.frozen ? "Unfreeze agents" : "Freeze all agents"} className={`ml-auto p-1.5 ${freeze.frozen ? "text-amber-300" : "text-ink-3"}`}>
          <Power size={18} strokeWidth={1.5} />
        </button>
        {onLogout && (
          <button type="button" onClick={onLogout} aria-label="Log out" className="p-1.5 text-ink-3">
            <LogOut size={18} strokeWidth={1.5} />
          </button>
        )}
      </header>

      <main className="px-4 pt-5 pb-24 sm:px-6 lg:ml-52 lg:px-9 lg:pt-6 lg:pb-6">
        <FrozenBanner freeze={freeze} onUnfreeze={toggleFreeze} />
        {children}
      </main>

      {/* Phone tab bar */}
      <nav
        aria-label="Main"
        className="fixed inset-x-0 bottom-0 z-20 flex border-t border-line bg-bg px-2 pt-1.5 pb-3 lg:hidden"
      >
        {SECTIONS.filter((s) => s.mobile).map(({ path, label, icon: Icon, mock }) => (
          <NavLink
            key={path}
            to={`/${path}`}
            className={({ isActive }) =>
              `flex flex-1 flex-col items-center gap-1 py-1.5 text-[11px] ${isActive ? "text-accent" : "text-ink-3"}`
            }
          >
            <span className="relative">
              <Icon size={20} strokeWidth={1.5} />
              {path === "approvals" && approvals > 0 && <span className="absolute -top-1 -right-1.5 h-2 w-2 rounded-full bg-amber-300" />}
              {mock && <MockDot className="absolute -top-1 -left-1.5" />}
            </span>
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
