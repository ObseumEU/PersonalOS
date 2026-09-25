import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getMe, logout, type Me } from "./api";
import Shell, { Mark } from "./components/Shell";
import AgentDetail from "./pages/AgentDetail";
import Approvals from "./pages/Approvals";
import Assistant from "./pages/Assistant";
import Automations from "./pages/Automations";
import Calendar from "./pages/Calendar";
import Chat from "./pages/Chat";
import Connectors from "./pages/Connectors";
import Files from "./pages/Files";
import InboxClarify from "./pages/InboxClarify";
import Login from "./pages/Login";
import Invite from "./pages/Invite";
import Notes from "./pages/Notes";
import Projects from "./pages/Projects";
import Team from "./pages/Team";
import System from "./pages/System";
import Tasks from "./pages/Tasks";
import Today from "./pages/Today";
import Tools from "./pages/Tools";
import Topics from "./pages/Topics";


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
  const invite = window.location.pathname.match(/^\/invite\/([\w-]+)/);
  if (invite && (!me.authenticated || !me.login_required)) return <Invite token={invite[1]} onJoined={setMe} />;
  if (!me.authenticated) return <Login onLoggedIn={setMe} />;

  return (
    <Shell onLogout={me.login_required ? () => logout().then(setMe) : undefined}>
      <Routes>
        <Route path="/" element={<Navigate to="/today" replace />} />
        <Route path="/today" element={<Today />} />
        <Route path="/tasks" element={<Tasks />} />
        <Route path="/tasks/inbox" element={<InboxClarify />} />
        <Route path="/team" element={<Team />} />
        <Route path="/team/:id" element={<AgentDetail />} />
        <Route path="/agents" element={<Navigate to="/team" replace />} />
        <Route path="/agents/:id" element={<AgentDetail />} />
        <Route path="/network" element={<KeepQuery to="/team" tab="network" />} />
        <Route path="/org" element={<Navigate to="/team?tab=structure" replace />} />
        <Route path="/board" element={<Navigate to="/team?tab=work" replace />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/connectors" element={<Connectors />} />
        <Route path="/automations" element={<Automations />} />
        <Route path="/tools" element={<Tools />} />
        <Route path="/assistant" element={<Assistant />} />
        <Route path="/calendar" element={<Calendar />} />
        <Route path="/chat" element={<Chat />} />
        <Route path="/files" element={<Files />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/projects/:slug" element={<Projects />} />
        <Route path="/topics" element={<Topics />} />
        <Route path="/topics/:slug" element={<Topics />} />
        <Route path="/notes" element={<Notes />} />
        <Route path="/system" element={<System />} />
        <Route path="/admin" element={<Navigate to="/system" replace />} />
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Routes>
    </Shell>
  );
}

/** An old path that now lives under a tab; its query (e.g. ?view=org) stays. */
function KeepQuery({ to, tab }: { to: string; tab: string }) {
  const q = new URLSearchParams(window.location.search);
  q.set("tab", tab);
  return <Navigate to={`${to}?${q}`} replace />;
}
