import { LogOut, Search, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { SECTIONS, type Section } from "../sections";
import Logo from "./Logo";

function NavItem({ s }: { s: Section }) {
  const Icon = s.icon;
  return (
    <NavLink
      to={`/${s.path}`}
      className={({ isActive }) =>
        `group relative flex items-center gap-3 rounded-xl px-3 py-2 text-[0.9rem] font-medium transition ${
          isActive
            ? "bg-surface-2 text-ink"
            : "text-ink-2 hover:bg-surface-2 hover:text-ink"
        }`
      }
    >
      {({ isActive }) => (
        <>
          <span
            className={`absolute top-1/2 left-0 h-5 w-1 -translate-y-1/2 rounded-r-full brand-gradient transition-opacity ${
              isActive ? "opacity-100" : "opacity-0"
            }`}
          />
          <Icon
            size={18}
            strokeWidth={isActive ? 2.3 : 1.9}
            className={isActive ? "text-brand" : ""}
          />
          {s.label}
        </>
      )}
    </NavLink>
  );
}

const group = (g: Section["group"]) => SECTIONS.filter((s) => s.group === g);

export default function Shell({
  children,
  onLogout,
}: {
  children: ReactNode;
  onLogout?: () => void;
}) {
  return (
    <div className="app-glow min-h-screen">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 hidden w-64 flex-col border-r border-line bg-[color-mix(in_oklab,var(--color-surface)_70%,transparent)] px-5 py-6 backdrop-blur-xl lg:flex">
        <div className="flex items-center gap-3 px-1">
          <Logo />
          <div>
            <div className="text-[0.95rem] font-semibold tracking-tight">PersonalOS</div>
            <div className="text-xs text-ink-3">Your life, organised</div>
          </div>
        </div>

        <button
          type="button"
          className="mt-6 flex items-center gap-2 rounded-xl border border-line bg-surface px-3 py-2 text-sm text-ink-3 transition hover:border-brand"
        >
          <Search size={16} />
          Search or ask…
          <kbd className="ml-auto rounded-md border border-line px-1.5 text-[0.7rem]">Ctrl K</kbd>
        </button>

        <nav className="mt-6 flex flex-1 flex-col gap-6 overflow-y-auto">
          <div className="space-y-1">
            <div className="px-3 pb-1 text-[0.7rem] font-semibold tracking-wider text-ink-3 uppercase">
              Daily
            </div>
            {group("daily").map((s) => (
              <NavItem key={s.path} s={s} />
            ))}
          </div>
          <div className="space-y-1">
            <div className="px-3 pb-1 text-[0.7rem] font-semibold tracking-wider text-ink-3 uppercase">
              Assistant
            </div>
            {group("assistant").map((s) => (
              <NavItem key={s.path} s={s} />
            ))}
          </div>
          <div className="mt-auto space-y-1">
            {group("system").map((s) => (
              <NavItem key={s.path} s={s} />
            ))}
          </div>
        </nav>

        <div className="mt-4 flex items-center gap-3 rounded-xl border border-line bg-surface p-2.5">
          <div className="grid h-8 w-8 place-items-center rounded-full bg-surface-2 text-ink-2">
            <UserRound size={16} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">Owner</div>
            <div className="text-xs text-ink-3">Single-user mode</div>
          </div>
          {onLogout && (
            <button
              type="button"
              onClick={onLogout}
              title="Log out"
              className="rounded-lg p-1.5 text-ink-3 transition hover:bg-surface-2 hover:text-ink"
            >
              <LogOut size={16} />
            </button>
          )}
        </div>
      </aside>

      {/* Mobile top bar */}
      <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-line bg-[color-mix(in_oklab,var(--color-bg)_80%,transparent)] px-4 py-3 backdrop-blur-xl lg:hidden">
        <Logo size={30} />
        <span className="font-semibold tracking-tight">PersonalOS</span>
        {onLogout && (
          <button type="button" onClick={onLogout} title="Log out" className="ml-auto p-1.5 text-ink-3">
            <LogOut size={18} />
          </button>
        )}
      </header>

      <main className="px-4 pt-6 pb-28 sm:px-8 lg:ml-64 lg:px-12 lg:pt-10 lg:pb-12">
        <div className="mx-auto max-w-6xl">{children}</div>
      </main>

      {/* Mobile tab bar */}
      <nav className="fixed inset-x-3 bottom-3 z-20 flex justify-between rounded-2xl border border-line bg-[color-mix(in_oklab,var(--color-surface)_85%,transparent)] p-1.5 shadow-xl backdrop-blur-xl lg:hidden">
        {SECTIONS.filter((s) => ["today", "tasks", "calendar", "files", "assistant"].includes(s.path)).map((s) => {
          const Icon = s.icon;
          return (
            <NavLink
              key={s.path}
              to={`/${s.path}`}
              className={({ isActive }) =>
                `flex flex-1 flex-col items-center gap-0.5 rounded-xl py-1.5 text-[0.65rem] font-medium ${
                  isActive ? "bg-surface-2 text-brand" : "text-ink-3"
                }`
              }
            >
              <Icon size={20} />
              {s.label}
            </NavLink>
          );
        })}
      </nav>
    </div>
  );
}
