import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getMe, logout, type Me } from "./api";
import Shell, { Mark } from "./components/Shell";
import AgentDetail from "./pages/AgentDetail";
import Agents from "./pages/Agents";
import Approvals from "./pages/Approvals";
import Assistant from "./pages/Assistant";
import Automations from "./pages/Automations";
import Board from "./pages/Board";
import ComingSoon from "./pages/ComingSoon";
import Connectors from "./pages/Connectors";
import InboxClarify from "./pages/InboxClarify";
import Login from "./pages/Login";
import NetworkPage from "./pages/Network";
import System from "./pages/System";
import Tasks from "./pages/Tasks";
import Today from "./pages/Today";
import { SECTIONS } from "./sections";

const BUILT = new Set(["today", "tasks", "assistant", "system", "agents", "network", "approvals", "connectors", "automations"]);

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMe().then(setMe, (e) => setError(String(e.message ?? e)));
  }, []);

  if (error || !me) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-bg p-6 text-center">
        <div className={error ? "" : "breathe"}>
          <Mark size={36} />
        </div>
        {error && <p className="cap text-red-400!">Cannot reach the API: {error}</p>}
      </div>
    );
  }
  if (!me.authenticated) return <Login onLoggedIn={setMe} />;

  return (
    <Shell onLogout={me.login_required ? () => logout().then(setMe) : undefined}>
      <Routes>
        <Route path="/" element={<Navigate to="/today" replace />} />
        <Route path="/today" element={<Today />} />
        <Route path="/tasks" element={<Tasks />} />
        <Route path="/tasks/inbox" element={<InboxClarify />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/agents/:id" element={<AgentDetail />} />
        <Route path="/network" element={<NetworkPage />} />
        <Route path="/board" element={<Board />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/connectors" element={<Connectors />} />
        <Route path="/automations" element={<Automations />} />
        <Route path="/assistant" element={<Assistant />} />
        <Route path="/system" element={<System />} />
        <Route path="/admin" element={<Navigate to="/system" replace />} />
        {SECTIONS.filter((s) => !BUILT.has(s.path)).map((s) => (
          <Route key={s.path} path={`/${s.path}`} element={<ComingSoon section={s} />} />
        ))}
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Routes>
    </Shell>
  );
}
