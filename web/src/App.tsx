import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getMe, logout, type Me } from "./api";
import Shell, { Mark } from "./components/Shell";
import SectionTabs from "./components/SectionTabs";
import { t } from "./i18n";
import { KNOWLEDGE_TABS, WORK_TABS } from "./sections";

// Every page is its own chunk, loaded when it is first opened: the first paint needs only the
// Shell (Reports bring the chart library, Znalosti the 3D graph, the task panel Markdown).
const AgentDetail = lazy(() => import("./pages/AgentDetail"));
const Approvals = lazy(() => import("./pages/Approvals"));
const AssistantChat = lazy(() => import("./pages/AssistantChat"));
const Automations = lazy(() => import("./pages/Automations"));
const Calendar = lazy(() => import("./pages/Calendar"));
const Chat = lazy(() => import("./pages/Chat"));
const Connectors = lazy(() => import("./pages/Connectors"));
const Credentials = lazy(() => import("./pages/Credentials"));
const Files = lazy(() => import("./pages/Files"));
const InboxClarify = lazy(() => import("./pages/InboxClarify"));
const Invite = lazy(() => import("./pages/Invite"));
const Knowledge = lazy(() => import("./pages/Knowledge"));
const Login = lazy(() => import("./pages/Login"));
const MobileAppSettings = lazy(() => import("./pages/MobileAppSettings"));
const Notes = lazy(() => import("./pages/Notes"));
const Projects = lazy(() => import("./pages/Projects"));
const Reports = lazy(() => import("./pages/Reports"));
const Settings = lazy(() => import("./pages/Settings"));
const System = lazy(() => import("./pages/System"));
const Tasks = lazy(() => import("./pages/Tasks"));
const Team = lazy(() => import("./pages/Team"));
const Today = lazy(() => import("./pages/Today"));
const Tools = lazy(() => import("./pages/Tools"));
const Topics = lazy(() => import("./pages/Topics"));
const WeeklyReview = lazy(() => import("./pages/WeeklyReview"));

/** While a page's chunk loads (once per page; then it is cached). */
function PageLoading() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center" aria-busy="true">
      <div className="breathe">
        <Mark size={28} />
      </div>
    </div>
  );
}

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
  if (invite && (!me.authenticated || !me.login_required))
    return (
      <Suspense fallback={<PageLoading />}>
        <Invite token={invite[1]} onJoined={setMe} />
      </Suspense>
    );
  if (!me.authenticated)
    return (
      <Suspense fallback={<PageLoading />}>
        <Login onLoggedIn={setMe} />
      </Suspense>
    );

  return (
    <Shell onLogout={me.login_required ? () => logout().then(setMe) : undefined}>
      <Suspense fallback={<PageLoading />}>
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
          <Route path="/settings/mobile" element={<MobileAppSettings />} />
          <Route path="/credentials" element={<Credentials />} />
          <Route path="/connectors" element={<Connectors />} />
          <Route path="/tools" element={<Tools />} />
          <Route path="/automations" element={<Automations />} />
          <Route path="/system" element={<System />} />
          <Route path="/admin" element={<Navigate to="/system" replace />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="/reports/:week" element={<Reports />} />
          <Route path="*" element={<Navigate to="/today" replace />} />
        </Routes>
      </Suspense>
    </Shell>
  );
}

function Work({ children }: { children: ReactNode }) {
  return (
    <SectionTabs tabs={WORK_TABS} label={t("nav.work")}>
      <Suspense fallback={<PageLoading />}>{children}</Suspense>
    </SectionTabs>
  );
}

function Know({ children }: { children: ReactNode }) {
  return (
    <SectionTabs tabs={KNOWLEDGE_TABS} label={t("nav.knowledge")}>
      <Suspense fallback={<PageLoading />}>{children}</Suspense>
    </SectionTabs>
  );
}
