import { LogOut } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { SUBSYSTEMS } from "../sample";
import { SECTIONS } from "../sections";

export function Mark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#6cc4dc" strokeWidth="1.4" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3" fill="#6cc4dc" />
      <path d="M12 1v4M12 19v4M1 12h4M19 12h4" />
    </svg>
  );
}

export default function Shell({ children, onLogout }: { children: ReactNode; onLogout?: () => void }) {
  return (
    <div className="min-h-screen bg-bg">
      {/* Desktop rail */}
      <aside className="fixed inset-y-0 left-0 hidden w-52 flex-col gap-6 border-r border-line px-3.5 pt-6 pb-5 lg:flex">
        <div className="flex items-center gap-2.5 px-2">
          <Mark />
          <span className="font-medium tracking-[-0.01em]">PersonalOS</span>
        </div>
        <nav aria-label="Main" className="flex flex-col gap-0.5">
          {SECTIONS.map(({ path, label, icon: Icon }) => (
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
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto flex flex-col gap-2 rounded-md border border-line p-3">
          <span className="cap flex items-center justify-between">
            SUBSYSTEMS <span className="rounded-sm border border-dashed border-ink-3 px-1 text-[9px]">SAMPLE</span>
          </span>
          {SUBSYSTEMS.map((s) => (
            <span key={s.name} className="flex items-center gap-2 text-[13px] text-ink-2">
              <span className="h-1.5 w-1.5 rounded-full bg-accent" />
              {s.name.split(" ")[0]}
              <span className="cap ml-auto">
                {s.value}
                {s.unit.startsWith("ms") ? "ms" : "s"}
              </span>
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
        {onLogout && (
          <button type="button" onClick={onLogout} aria-label="Log out" className="ml-auto p-1.5 text-ink-3">
            <LogOut size={18} strokeWidth={1.5} />
          </button>
        )}
      </header>

      <main className="px-4 pt-5 pb-24 sm:px-6 lg:ml-52 lg:px-9 lg:pt-6 lg:pb-6">{children}</main>

      {/* Phone tab bar */}
      <nav
        aria-label="Main"
        className="fixed inset-x-0 bottom-0 z-20 flex border-t border-line bg-bg px-2 pt-1.5 pb-3 lg:hidden"
      >
        {SECTIONS.filter((s) => s.mobile).map(({ path, label, icon: Icon }) => (
          <NavLink
            key={path}
            to={`/${path}`}
            className={({ isActive }) =>
              `flex flex-1 flex-col items-center gap-1 py-1.5 text-[11px] ${isActive ? "text-accent" : "text-ink-3"}`
            }
          >
            <Icon size={20} strokeWidth={1.5} />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
