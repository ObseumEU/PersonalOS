import { ArrowRight } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { acceptInvite, api, type Me } from "../api";
import { Mark } from "../components/Shell";

/** Accept an invitation: the invited person sets a password and is signed in. */
export default function Invite({ token, onJoined }: { token: string; onJoined: (me: Me) => void }) {
  const [info, setInfo] = useState<{ name: string; email: string } | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<{ name: string; email: string }>(`/api/auth/invite/${token}`).then(setInfo, (e) => setError(e.message));
  }, [token]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    acceptInvite(token, password).then((me) => {
      window.history.replaceState(null, "", "/today");
      onJoined(me);
    }, (e2) => setError(e2.message));
  };
  return (
    <div className="measure-grid flex min-h-screen items-center justify-center bg-bg p-4">
      <form onSubmit={submit} className="panel fade-in w-full max-w-sm p-7">
        <div className="flex items-center gap-2.5">
          <Mark />
          <span className="font-medium">PersonalOS</span>
        </div>
        <h1 className="mt-6 text-2xl font-light tracking-[-0.02em]">{info ? `Welcome, ${info.name}` : "Invitation"}</h1>
        {info && <p className="cap mt-2">{info.email} · set your password (at least 10 characters)</p>}
        {info && (
          <>
            <label htmlFor="new-password" className="cap mt-6 block">
              PASSWORD
            </label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-2 h-11 w-full rounded-md border border-line bg-bg px-3 outline-none focus:border-accent"
            />
            <button
              disabled={password.length < 10}
              className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-md border border-accent bg-accent/10 text-sm font-medium text-accent transition hover:bg-accent/20 disabled:opacity-40"
            >
              Join <ArrowRight size={15} />
            </button>
          </>
        )}
        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      </form>
    </div>
  );
}
