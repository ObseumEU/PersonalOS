import { ArrowRight, ExternalLink, Gauge, Inbox, ListChecks, LogOut, MessagesSquare, MoreHorizontal, RefreshCw, Settings, WifiOff } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ApiError, api, getMe, logout, type Me } from "../api";
import { OverlayHost } from "../components/overlay";
import { useCurrentRef } from "../refStore";
import { t } from "../i18n/core";
import { useNeedsMe } from "../needsMeApi";
import ChatList from "./ChatList";
import { applyUpdate, isStandalone, setBadge, useUpdateReady } from "./pwa";
import { TopBar } from "./ui";

// Loaded when used: the full task panel (Markdown and all) and the settings (the QR code).
// The start screen (the chat list) is in the entry; the other screens load on first use and are
// fetched when the phone is idle, so a tap is instant and the service worker has them offline.
const loadConversation = () => import("./Conversation");
const loadNeeds = () => import("./Needs");
const loadTasks = () => import("./Tasks");
const Conversation = lazy(loadConversation);
const Needs = lazy(loadNeeds);
const Tasks = lazy(loadTasks);
const TaskSheetHost = lazy(() => import("../components/tasks/TaskSheet"));
const MobileSettings = lazy(() => import("./Settings"));
const ReportPage = lazy(() => import("../pages/ReportPage"));
// Firma: the company scorecard (loads on first open).
const Company = lazy(() => import("../pages/Company"));
const RefPreviewHost = lazy(() => import("../components/RefPreview").then((m) => ({ default: m.RefPreviewHost })));

/** The reference preview (a note, a message, a source) loads the first time one is opened. */
function RefPreviewSlot() {
  const cur = useCurrentRef();
  return cur ? (
    <Suspense fallback={null}>
      <RefPreviewHost />
    </Suspense>
  ) : null;
}

function Mark({ size = 40 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#6cc4dc" strokeWidth="1.4" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="3" fill="#6cc4dc" />
      <path d="M12 1v4M12 19v4M1 12h4M19 12h4" />
    </svg>
  );
}

/** Outside the VPN the public proxy answers 403; no network at all is a TypeError. Both: offline. */
const isOffline = (e: unknown) => !(e instanceof ApiError) || e.status === 403 || e.status >= 500;

function Offline({ onRetry, busy }: { onRetry: () => void; busy: boolean }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-4 bg-bg px-8 text-center">
      <WifiOff size={36} className="text-ink-2" strokeWidth={1.4} />
      <h1 className="text-lg font-medium">{t("m.offline.title")}</h1>
      <p className="max-w-xs text-[15px] leading-relaxed text-ink-2">{t("m.offline.body")}</p>
      <button onClick={onRetry} disabled={busy} className="btn-accent mt-2 h-12! px-6! text-[15px]!">
        <RefreshCw size={16} className={busy ? "animate-spin" : ""} /> {t("m.offline.retry")}
      </button>
    </div>
  );
}

function Login({ onLoggedIn }: { onLoggedIn: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // app: true keeps this device signed in for the app's lifetime (pos.devices); revocable in Nastavení.
      onLoggedIn(await api<Me>("/api/auth/login", { method: "POST", body: JSON.stringify({ password, app: true, ...(email.trim() ? { email: email.trim() } : {}) }) }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const field = "mt-1.5 h-12 w-full rounded-lg border border-line bg-surface px-3 text-[16px] outline-none focus:border-accent";
  return (
    <form onSubmit={submit} className="flex min-h-dvh flex-col justify-center gap-4 bg-bg px-6">
      <div className="flex items-center gap-3">
        <Mark size={32} />
        <span className="text-lg font-medium">PersonalOS</span>
      </div>
      <h1 className="text-2xl font-light">{t("login.title")}</h1>
      <label className="text-[13px] text-ink-2">
        {t("login.email")} · {t("login.email_hint")}
        <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} className={field} />
      </label>
      <label className="text-[13px] text-ink-2">
        {t("login.password")}
        <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} className={field} />
      </label>
      {error && <p className="text-sm text-red-400">{error}</p>}
      <button disabled={busy || !password} className="flex h-12 items-center justify-center gap-2 rounded-lg border border-accent bg-accent/10 text-[16px] font-medium text-accent disabled:opacity-40">
        {busy ? t("login.busy") : t("login.submit")} {!busy && <ArrowRight size={16} />}
      </button>
      <p className="text-[13px] leading-relaxed text-ink-2">{t("m.login.app")}</p>
    </form>
  );
}

function TabBar({ needs }: { needs: number }) {
  const loc = useLocation();
  const tabs = [
    { to: "/m", label: t("m.tab.chat"), Icon: MessagesSquare, active: loc.pathname === "/m" || loc.pathname.startsWith("/m/chat") },
    { to: "/m/needs", label: t("m.tab.needs"), Icon: Inbox, active: loc.pathname.startsWith("/m/needs"), badge: needs },
    { to: "/m/tasks", label: t("m.tab.tasks"), Icon: ListChecks, active: loc.pathname.startsWith("/m/tasks") },
    { to: "/m/more", label: t("m.tab.more"), Icon: MoreHorizontal, active: loc.pathname.startsWith("/m/more") || loc.pathname.startsWith("/m/settings") || loc.pathname.startsWith("/m/company") },
  ];
  return (
    <nav aria-label={t("m.tabs")} className="fixed inset-x-0 bottom-0 z-20 flex border-t border-line bg-bg/95 pb-[env(safe-area-inset-bottom)] backdrop-blur">
      {tabs.map(({ to, label, Icon, active, badge }) => (
        <Link key={to} to={to} aria-current={active ? "page" : undefined} className={`flex h-16 min-w-0 flex-1 flex-col items-center justify-center gap-1 text-[12px] ${active ? "text-accent" : "text-ink-2"}`}>
          <span className="relative">
            <Icon size={22} strokeWidth={1.6} />
            {!!badge && (
              <span className="absolute -top-1.5 -right-3 min-w-5 rounded-full bg-amber-300 px-1 text-center font-mono text-[12px] leading-5 text-bg">{badge}</span>
            )}
          </span>
          <span className="truncate">{label}</span>
        </Link>
      ))}
    </nav>
  );
}

function More({ onLogout }: { onLogout?: () => void }) {
  const row = "flex min-h-14 items-center gap-3 border-b border-line px-4 text-[16px] active:bg-raised";
  return (
    <div className="flex flex-col">
      <TopBar title={t("m.more.title")} />
      <Link to="/m/company" className={row}>
        <Gauge size={20} className="text-ink-2" />
        <span className="flex flex-col">
          {t("m.more.company")}
          <span className="text-[13px] text-ink-2">{t("m.more.company_hint")}</span>
        </span>
      </Link>
      <Link to="/m/settings" className={row}>
        <Settings size={20} className="text-ink-2" />
        <span className="flex flex-col">
          {t("nav.settings")}
          <span className="text-[13px] text-ink-2">{t("m.more.settings")}</span>
        </span>
      </Link>
      <a href="/today" className={row}>
        <ExternalLink size={20} className="text-ink-2" />
        <span className="flex flex-col">
          {t("m.more.full")}
          <span className="text-[13px] text-ink-2">{t("m.more.full_hint")}</span>
        </span>
      </a>
      {onLogout && (
        <button onClick={onLogout} className={`${row} text-left text-ink-2`}>
          <LogOut size={20} /> {t("m.more.logout")}
        </button>
      )}
    </div>
  );
}

function SettingsScreen() {
  const navigate = useNavigate();
  return (
    <div className="flex flex-col">
      <TopBar title={t("nav.mobile")} back={() => navigate("/m/more")} />
      <div className="p-3">
        <Suspense fallback={<p className="p-4 text-sm text-ink-2">{t("act.loading")}</p>}>
          <MobileSettings />
        </Suspense>
      </div>
    </div>
  );
}

function UpdateToast() {
  const ready = useUpdateReady();
  if (!ready) return null;
  return (
    <div className="fixed inset-x-3 top-[calc(env(safe-area-inset-top)+8px)] z-50 flex items-center gap-3 rounded-xl border border-accent/60 bg-surface px-4 py-2.5 shadow-lg">
      <RefreshCw size={16} className="text-accent" />
      <span className="flex-1 text-[15px]">{t("m.update")}</span>
      <button onClick={applyUpdate} className="h-10 rounded-lg bg-accent px-4 text-[15px] font-medium text-bg">
        {t("m.update.reload")}
      </button>
    </div>
  );
}

/** Not a /m address (a link from the task panel into the full app): leave the app shell. */
function Outside() {
  const loc = useLocation();
  useEffect(() => {
    window.location.assign(loc.pathname + loc.search);
  }, [loc]);
  return null;
}

function Shell({ onLogout }: { onLogout?: () => void }) {
  const needs = useNeedsMe();
  const loc = useLocation();
  const navigate = useNavigate();
  const count = needs?.count ?? 0;
  useEffect(() => setBadge(count), [count]);
  // A tap on a notification while the app is open (the service worker posts the address).
  useEffect(() => {
    const open = (e: Event) => navigate((e as CustomEvent<string>).detail);
    window.addEventListener("pos:open", open);
    return () => window.removeEventListener("pos:open", open);
  }, [navigate]);
  useEffect(() => {
    const idle = (window as Window & { requestIdleCallback?: (f: () => void) => void }).requestIdleCallback ?? ((f: () => void) => setTimeout(f, 1500));
    idle(() => void Promise.all([loadConversation(), loadNeeds(), loadTasks()]).catch(() => undefined));
  }, []);
  const inConversation = /^\/m\/chat\/\d+/.test(loc.pathname);
  return (
    <div className="min-h-dvh bg-bg">
      <main className={inConversation ? "" : "pb-[calc(64px+env(safe-area-inset-bottom))]"}>
        <Suspense fallback={<p className="p-4 text-sm text-ink-2">{t("act.loading")}</p>}>
        <Routes>
          <Route path="/m" element={<ChatList />} />
          <Route path="/m/chat/:id" element={<Conversation />} />
          <Route path="/m/needs" element={<Needs />} />
          <Route path="/m/tasks" element={<Tasks />} />
          <Route path="/m/more" element={<More onLogout={onLogout} />} />
          <Route path="/m/settings" element={<SettingsScreen />} />
          <Route path="/m/company" element={<Company />} />
          <Route path="/m/report/:ref" element={<Suspense fallback={null}><ReportPage /></Suspense>} />
          <Route path="/m/*" element={<Navigate to="/m" replace />} />
          <Route path="*" element={<Outside />} />
        </Routes>
        </Suspense>
      </main>
      {!inConversation && <TabBar needs={count} />}
      {/* The task panel over any screen: ?task=T-123 (from a list, a message, "Čeká na tebe"). */}
      {new URLSearchParams(loc.search).has("task") || new URLSearchParams(loc.search).has("approval") ? (
        <Suspense fallback={null}>
          <TaskSheetHost />
        </Suspense>
      ) : null}
      <RefPreviewSlot />
      <UpdateToast />
      <OverlayHost />
    </div>
  );
}

export default function MobileApp() {
  const [me, setMe] = useState<Me | null>(null);
  const [offline, setOffline] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    setBusy(true);
    getMe()
      .then((m) => {
        setMe(m);
        setOffline(false);
      }, (e) => {
        if (isOffline(e)) setOffline(true);
      })
      .finally(() => setBusy(false));
  }, []);
  useEffect(load, [load]);
  // Back online (VPN on again): retry by itself.
  useEffect(() => {
    window.addEventListener("online", load);
    return () => window.removeEventListener("online", load);
  }, [load]);
  // The installed app keeps its device signed in for long (pos.devices).
  useEffect(() => {
    if (me?.authenticated && me.login_required && isStandalone()) api("/api/auth/devices/current/app", { method: "POST" }).catch(() => undefined);
  }, [me]);

  if (offline && !me) return <Offline onRetry={load} busy={busy} />;
  if (!me)
    return (
      <div className="flex min-h-dvh items-center justify-center bg-bg">
        <span className="breathe">
          <Mark />
        </span>
      </div>
    );
  if (!me.authenticated) return <Login onLoggedIn={setMe} />;
  return <Shell onLogout={me.login_required ? () => logout().then(setMe) : undefined} />;
}
