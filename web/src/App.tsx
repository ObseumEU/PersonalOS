import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getMe, logout, type Me } from "./api";
import Logo from "./components/Logo";
import Shell from "./components/Shell";
import ComingSoon from "./pages/ComingSoon";
import Login from "./pages/Login";
import Today from "./pages/Today";
import { SECTIONS } from "./sections";

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMe().then(setMe, (e) => setError(String(e.message ?? e)));
  }, []);

  if (error || !me) {
    return (
      <div className="app-glow flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
        <div className={error ? "" : "animate-pulse"}>
          <Logo size={48} />
        </div>
        {error && <p className="text-sm text-red-500">Cannot reach the API: {error}</p>}
      </div>
    );
  }
  if (!me.authenticated) return <Login onLoggedIn={setMe} />;

  return (
    <Shell onLogout={me.login_required ? () => logout().then(setMe) : undefined}>
      <Routes>
        <Route path="/" element={<Navigate to="/today" replace />} />
        <Route path="/today" element={<Today />} />
        {SECTIONS.filter((s) => s.path !== "today").map((s) => (
          <Route key={s.path} path={`/${s.path}`} element={<ComingSoon section={s} />} />
        ))}
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Routes>
    </Shell>
  );
}
