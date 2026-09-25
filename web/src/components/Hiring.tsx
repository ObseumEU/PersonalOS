import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel } from "./ui";

type Hire = {
  id: number;
  name: string;
  purpose: string;
  role: string | null;
  lead_name: string | null;
  requested_by_name: string | null;
  decider_name: string | null;
  needs_owner: string | null;
  status: "pending" | "approved" | "rejected";
  hr_verdict: { allowed?: boolean; decision?: string; reason?: string } | null;
  created_at: string;
};

const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

/** Hire a colleague: one request, HR's limits first, the lead (or the owner) decides; 7 days' probation. */
export default function HiringPanel({ members, onHired }: { members: { id: number; name: string; kind: string }[]; onHired: () => void }) {
  const [hires, setHires] = useState<Hire[]>([]);
  const [form, setForm] = useState({ name: "", purpose: "", role: "", lead: "", instructions: "" });
  const [error, setError] = useState<string | null>(null);
  const load = () => api<Hire[]>("/api/hires").then(setHires, (e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const decide = (id: number, approve: boolean) => {
    const note = approve ? "" : window.prompt("Why not?") ?? "";
    post<{ api_key?: string }>(`/api/hires/${id}/decide`, { approve, note }).then(
      (r) => {
        if (r.api_key) window.alert(`Hired. The agent's key (shown once, for its worker): ${r.api_key}`);
        load();
        onHired();
      },
      (e) => setError(e.message),
    );
  };
  const pending = hires.filter((h) => h.status === "pending");
  return (
    <Panel fig="HIRE" title="Hire a colleague" right={`${pending.length} pending · the lead decides · 7 days' probation`}>
      <form
        className="grid grid-cols-1 gap-2 border-b border-line p-4 md:grid-cols-4"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          post("/api/hires", { ...form, lead: form.lead ? Number(form.lead) : null, role: form.role || null }).then(
            () => {
              setForm({ name: "", purpose: "", role: "", lead: "", instructions: "" });
              load();
            },
            (e2) => setError(e2.message),
          );
        }}
      >
        <input className={input} placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-label="Name" />
        <input className={`${input} md:col-span-2`} placeholder="Purpose: what it is for" value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} aria-label="Purpose" />
        <input className={input} placeholder="Role (optional)" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} aria-label="Role" />
        <select className={input} value={form.lead} onChange={(e) => setForm({ ...form, lead: e.target.value })} aria-label="Lead">
          <option value="">reports to the Project manager</option>
          {members.map((m) => (
            <option key={m.id} value={m.id}>
              reports to {m.name}
            </option>
          ))}
        </select>
        <textarea
          rows={2}
          className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent md:col-span-2"
          placeholder="Draft instructions (optional)"
          value={form.instructions}
          onChange={(e) => setForm({ ...form, instructions: e.target.value })}
          aria-label="Instructions"
        />
        <button type="submit" className="btn-accent self-start" disabled={!form.name.trim() || !form.purpose.trim()}>
          Ask to hire
        </button>
      </form>
      {hires.slice(0, 12).map((h) => (
        <div key={h.id} className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0">
          <span className="font-medium">{h.name}</span>
          <span className="cap truncate">{h.purpose}</span>
          <span className="cap">
            → {h.lead_name} · asked by {h.requested_by_name} · decides {h.decider_name}
          </span>
          {h.needs_owner && <span className="cap text-amber-300!">{h.needs_owner}</span>}
          <span className="cap ml-auto">{h.status}</span>
          {h.status === "pending" && (
            <>
              <button type="button" className="btn" onClick={() => decide(h.id, true)}>
                Approve
              </button>
              <button type="button" className="btn" onClick={() => decide(h.id, false)}>
                Reject
              </button>
            </>
          )}
        </div>
      ))}
      {error && <p className="cap px-4 py-2 text-red-400!">{error}</p>}
    </Panel>
  );
}
