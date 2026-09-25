import { ArrowRight } from "lucide-react";
import { useState, type FormEvent } from "react";
import { login, type Me } from "../api";
import { Mark } from "../components/Shell";

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
    <div className="measure-grid flex min-h-screen items-center justify-center bg-bg p-4">
      <form onSubmit={submit} className="panel fade-in w-full max-w-sm p-7">
        <div className="flex items-center gap-2.5">
          <Mark />
          <span className="font-medium">PersonalOS</span>
        </div>
        <h1 className="mt-6 text-2xl font-light tracking-[-0.02em]">Sign in</h1>
        <label htmlFor="password" className="cap mt-6 block">
          PASSWORD
        </label>
        <input
          id="password"
          type="password"
          autoFocus
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mt-2 h-11 w-full rounded-md border border-line bg-bg px-3 outline-none focus:border-accent"
        />
        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
        <button
          disabled={busy || !password}
          className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-md border border-accent bg-accent/10 text-sm font-medium text-accent transition hover:bg-accent/20 disabled:opacity-40"
        >
          {busy ? "Signing in…" : "Sign in"} {!busy && <ArrowRight size={15} />}
        </button>
      </form>
    </div>
  );
}
