import { ArrowRight, Lock } from "lucide-react";
import { useState, type FormEvent } from "react";
import { login, type Me } from "../api";
import Logo from "../components/Logo";

export default function Login({ onLoggedIn }: { onLoggedIn: (me: Me) => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLoggedIn(await login(password));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app-glow flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="card rise w-full max-w-sm p-8">
        <Logo size={48} />
        <h1 className="mt-6 text-2xl font-semibold tracking-tight">Welcome back</h1>
        <p className="mt-1 text-sm text-ink-2">Sign in to your PersonalOS.</p>

        <label className="mt-8 flex items-center gap-3 rounded-xl border border-line bg-surface px-3.5 py-3 transition focus-within:border-brand focus-within:ring-4 focus-within:ring-brand/15">
          <Lock size={16} className="text-ink-3" />
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-ink-3"
          />
        </label>
        {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

        <button
          disabled={busy || !password}
          className="brand-gradient mt-6 flex w-full items-center justify-center gap-2 rounded-xl py-3 font-medium text-white shadow-lg transition hover:brightness-110 disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
          {!busy && <ArrowRight size={16} />}
        </button>
      </form>
    </div>
  );
}
