import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getMe, logout, type Me } from "./api";
import Shell, { Mark } from "./components/Shell";
import AgentDetail from "./pages/AgentDetail";
import Approvals from "./pages/Approvals";
import AssistantChat from "./pages/AssistantChat";
import Automations from "./pages/Automations";
import Calendar from "./pages/Calendar";
import Chat from "./pages/Chat";
import Connectors from "./pages/Connectors";
import Credentials from "./pages/Credentials";
import Files from "./pages/Files";
import InboxClarify from "./pages/InboxClarify";
import Login from "./pages/Login";
import Invite from "./pages/Invite";
import Knowledge from "./pages/Knowledge";
import Settings from "./pages/Settings";
import SectionTabs from "./components/SectionTabs";
import { t } from "./i18n";
import { KNOWLEDGE_TABS, WORK_TABS } from "./sections";
import Notes from "./pages/Notes";
import Projects from "./pages/Projects";
import Team from "./pages/Team";
import WeeklyReview from "./pages/WeeklyReview";
import System from "./pages/System";
import Tasks from "./pages/Tasks";
import Today from "./pages/Today";
import Tools from "./pages/Tools";
import Topics from "./pages/Topics";

// Reports carry the chart library: loaded when opened, not with every page.
const Reports = lazy(() => import("./pages/Reports"));


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
        {error && <p className="text-sm text-red-400">{t("api.unreachable", { error })}</p>}
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
        {/* Práce: Úkoly · Projekty · Kalendář */}
        <Route path="/work" element={<Navigate to="/tasks" replace />} />
        <Route path="/tasks" element={<Work><Tasks /></Work>} />
        <Route path="/tasks/inbox" element={<InboxClarify />} />
        <Route path="/tasks/:ref" element={<Work><Tasks /></Work>} />
        <Route path="/projects" element={<Work><Projects /></Work>} />
        <Route path="/projects/:slug" element={<Work><Projects /></Work>} />
        <Route path="/calendar" element={<Work><Calendar /></Work>} />
        <Route path="/weekly-review" element={<WeeklyReview />} />
        {/* Znalosti: one list, with Soubory · Poznámky · Témata as filters and their detail pages */}
        <Route path="/knowledge" element={<Know><Knowledge /></Know>} />
        <Route path="/files" element={<Know><Files /></Know>} />
        <Route path="/notes" element={<Know><Notes /></Know>} />
        <Route path="/topics" element={<Know><Topics /></Know>} />
        <Route path="/topics/:slug" element={<Know><Topics /></Know>} />
        {/* Tým */}
        <Route path="/team" element={<Team />} />
        <Route path="/team/:id" element={<AgentDetail />} />
        <Route path="/agents" element={<Navigate to="/team" replace />} />
        <Route path="/agents/:id" element={<AgentDetail />} />
        <Route path="/network" element={<Navigate to="/team?tab=structure" replace />} />
        <Route path="/org" element={<Navigate to="/team?tab=structure" replace />} />
        <Route path="/board" element={<Navigate to="/team?tab=work" replace />} />
        <Route path="/chat" element={<Chat />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/assistant" element={<AssistantChat />} />
        {/* Nastavení */}
        <Route path="/settings" element={<Settings />} />
        <Route path="/credentials" element={<Credentials />} />
        <Route path="/connectors" element={<Connectors />} />
        <Route path="/tools" element={<Tools />} />
        <Route path="/automations" element={<Automations />} />
        <Route path="/system" element={<System />} />
        <Route path="/admin" element={<Navigate to="/system" replace />} />
        <Route path="/reports" element={<Suspense fallback={null}><Reports /></Suspense>} />
        <Route path="/reports/:week" element={<Suspense fallback={null}><Reports /></Suspense>} />
        <Route path="*" element={<Navigate to="/today" replace />} />
      </Routes>
    </Shell>
  );
}

function Work({ children }: { children: ReactNode }) {
  return <SectionTabs tabs={WORK_TABS} label={t("nav.work")}>{children}</SectionTabs>;
}

function Know({ children }: { children: ReactNode }) {
  return <SectionTabs tabs={KNOWLEDGE_TABS} label={t("nav.knowledge")}>{children}</SectionTabs>;
}
