import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { getMe, logout, type Me } from "./api";
import Login from "./pages/Login";
import Placeholder from "./pages/Placeholder";

// Sections from docs/PLAN.md §3. Each gets its real page in a later phase.
const SECTIONS = [
  { path: "today", label: "Today", phase: 4 },
  { path: "files", label: "Files", phase: 2 },
  { path: "topics", label: "Topics", phase: 2 },
  { path: "tasks", label: "Tasks", phase: 2 },
  { path: "calendar", label: "Calendar", phase: 4 },
  { path: "notes", label: "Notes", phase: 2 },
  { path: "assistant", label: "Assistant", phase: 3 },
  { path: "admin", label: "Admin", phase: 5 },
];

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMe().then(setMe, (e) => setError(String(e.message ?? e)));
  }, []);

  if (error) return <p className="p-6 text-red-600">Cannot reach the API: {error}</p>;
  if (!me) return <p className="p-6 text-slate-500">Loading…</p>;
  if (!me.authenticated) return <Login onLoggedIn={setMe} />;

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 text-slate-900 md:flex-row">
      <nav className="flex gap-1 overflow-x-auto border-b bg-white p-2 md:w-52 md:flex-col md:border-r md:border-b-0 md:p-3">
        <span className="px-3 py-2 font-semibold">PersonalOS</span>
        {SECTIONS.map((s) => (
          <NavLink
            key={s.path}
            to={`/${s.path}`}
            className={({ isActive }) =>
              `rounded-md px-3 py-2 text-sm whitespace-nowrap ${
                isActive ? "bg-slate-900 text-white" : "hover:bg-slate-100"
              }`
            }
          >
            {s.label}
          </NavLink>
        ))}
        {me.login_required && (
          <button
            className="rounded-md px-3 py-2 text-left text-sm text-slate-500 hover:bg-slate-100 md:mt-auto"
            onClick={() => logout().then(setMe)}
          >
            Log out
          </button>
        )}
      </nav>
      <main className="flex-1 p-6">
        <Routes>
          <Route path="/" element={<Navigate to="/today" replace />} />
          {SECTIONS.map((s) => (
            <Route
              key={s.path}
              path={`/${s.path}`}
              element={<Placeholder title={s.label} phase={s.phase} />}
            />
          ))}
          <Route path="*" element={<Navigate to="/today" replace />} />
        </Routes>
      </main>
    </div>
  );
}
