import { Check, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { PageHeader, Panel } from "../components/ui";
import { ago } from "./Agents";

type Finding = { kind: string; file: string; line: number | null; detail: string };
type Publication = {
  id: number;
  tool: string;
  from_name: string | null;
  version: string;
  status: "pending" | "approved" | "rejected" | "published";
  findings: Finding[];
  created_at: string;
  decided_at: string | null;
};
type Tool = {
  id: string;
  name: string;
  scope: string;
  path: string;
  kind: "script" | "mcp" | "skill" | null;
  description: string;
  owner: string;
  visibility: string;
  version: string;
  entry: string;
  tests: string;
  permissions_needed: string[];
  outbound: boolean;
  valid: boolean;
  usage: { uses: number; ok: number; failed: number; users: number; last_at: string | null };
  review: { ok: boolean; findings: Finding[] };
  publication: Publication | null;
  blocked: boolean;
};
type Library = { tools: Tool[]; publications: Publication[] };

const GRID = "grid grid-cols-[minmax(0,1.6fr)_70px_minmax(0,0.9fr)_70px_90px_minmax(0,1fr)] items-center gap-3";

const where = (f: Finding) => `${f.file}${f.line ? `:${f.line}` : ""}`;

function ReviewCell({ t }: { t: Tool }) {
  if (t.blocked) return <span className="cap text-red-400!">rejected by owner</span>;
  if (!t.review.ok)
    return (
      <span className="cap truncate text-red-400!" title={t.review.findings.map((f) => `${f.kind}: ${where(f)} ${f.detail}`).join("\n")}>
        {t.review.findings.length} finding{t.review.findings.length === 1 ? "" : "s"}
      </span>
    );
  return <span className="cap text-accent!">clean{t.outbound ? " · outbound, needs approval" : ""}</span>;
}

function ToolRows({ items, open, setOpen }: { items: Tool[]; open: string | null; setOpen: (id: string | null) => void }) {
  return (
    <>
      <div className={`${GRID} border-b border-line px-4 py-2`}>
        {["TOOL", "KIND", "OWNER", "VERSION", "USES", "REVIEW"].map((h) => (
          <span key={h} className="cap">
            {h}
          </span>
        ))}
      </div>
      {items.length === 0 && <p className="cap p-4">None yet.</p>}
      {items.map((t) => (
        <div key={t.id} className="border-b border-line">
          <button
            className={`${GRID} w-full px-4 py-2.5 text-left text-[13px] hover:bg-raised ${t.valid ? "" : "opacity-60"}`}
            onClick={() => setOpen(open === t.id ? null : t.id)}
            aria-expanded={open === t.id}
          >
            <span className="flex min-w-0 flex-col">
              <span className="truncate">{t.name}</span>
              <span className="cap truncate">{t.description}</span>
            </span>
            <span className="cap">{t.kind ?? "?"}</span>
            <span className="truncate">{t.owner}</span>
            <span className="font-mono text-xs">{t.version}</span>
            <span className="cap" title={t.usage.last_at ? `last used ${ago(t.usage.last_at)}` : "never used"}>
              {t.usage.uses}
              {t.usage.failed ? ` · ${t.usage.failed} failed` : ""}
            </span>
            <ReviewCell t={t} />
          </button>
          {open === t.id && (
            <div className="flex flex-col gap-1.5 bg-raised/40 px-4 py-3 text-xs text-ink-2">
              <span>
                <span className="cap">PATH</span> <span className="font-mono">{t.path}/{t.entry}</span>
              </span>
              <span>
                <span className="cap">TESTS</span> <span className="font-mono">{t.tests}</span>
              </span>
              <span>
                <span className="cap">PERMISSIONS</span> {t.permissions_needed.length ? t.permissions_needed.join(", ") : "none"}
                {" · "}
                <span className="cap">OUTBOUND</span> {t.outbound ? "yes (approval per use)" : "no"}
              </span>
              {t.usage.uses > 0 && (
                <span>
                  <span className="cap">USAGE</span> {t.usage.ok} ok of {t.usage.uses} by {t.usage.users} member(s), last{" "}
                  {t.usage.last_at ? ago(t.usage.last_at) : "never"}
                </span>
              )}
              {t.publication && (
                <span>
                  <span className="cap">LAST PUBLICATION</span> #{t.publication.id} v{t.publication.version} {t.publication.status}
                </span>
              )}
              {t.review.findings.map((f, i) => (
                <span key={i} className="text-red-400">
                  {f.kind}: <span className="font-mono">{where(f)}</span> {f.detail}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
    </>
  );
}

export default function Tools() {
  const [lib, setLib] = useState<Library | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Library>("/api/tools").then(setLib, (e) => setError(e.message));
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);
  const decide = (id: number, approve: boolean) =>
    api(`/api/tools/publications/${id}/decide`, { method: "POST", body: JSON.stringify({ approve }) }).then(
      () => {
        setError(null);
        load();
      },
      (e) => setError(e.message),
    );

  const shared = lib?.tools.filter((t) => t.scope === "shared") ?? [];
  const personal = lib?.tools.filter((t) => t.scope !== "shared") ?? [];
  const pending = lib?.publications.filter((p) => p.status === "pending").length ?? 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="TOOLS · LIBRARY · SHARED AND PERSONAL"
        title="Tools"
        sub="Scripts, MCP tools and skills that agents write for themselves and share with the team. Shared tools pass the deployer and a guard review: no secrets, no outbound calls without approval, no permission escalation."
      />
      {error && <p className="cap text-red-400!">{error}</p>}

      <Panel fig="QUEUE" title="Publication queue" right={pending ? `${pending} waiting for you` : "nothing waiting"}>
        <div className="grid grid-cols-[50px_minmax(0,1fr)_minmax(0,0.8fr)_70px_100px_minmax(0,1.2fr)_140px] gap-3 border-b border-line px-4 py-2">
          {["#", "TOOL", "FROM", "VERSION", "STATUS", "GUARD REVIEW", ""].map((h) => (
            <span key={h} className="cap">
              {h}
            </span>
          ))}
        </div>
        {lib?.publications.length === 0 && <p className="cap p-4">No tool has been proposed for the team yet.</p>}
        {lib?.publications.map((p) => (
          <div
            key={p.id}
            className={`grid grid-cols-[50px_minmax(0,1fr)_minmax(0,0.8fr)_70px_100px_minmax(0,1.2fr)_140px] items-center gap-3 border-b border-line px-4 py-2.5 text-[13px] ${p.status === "pending" ? "" : "opacity-60"}`}
          >
            <span className="cap">{p.id}</span>
            <span className="flex min-w-0 flex-col">
              <span className="truncate">{p.tool}</span>
              <span className="cap">{ago(p.created_at)}</span>
            </span>
            <span className="truncate">{p.from_name ?? "?"}</span>
            <span className="font-mono text-xs">{p.version}</span>
            <span className={`cap ${p.status === "rejected" ? "text-red-400!" : p.status === "pending" ? "" : "text-accent!"}`}>{p.status}</span>
            <span
              className={`cap truncate ${p.findings.length ? "text-red-400!" : ""}`}
              title={p.findings.map((f) => `${f.kind}: ${where(f)} ${f.detail}`).join("\n")}
            >
              {p.findings.length ? p.findings.map((f) => `${f.kind} (${where(f)})`).join(", ") : "clean"}
            </span>
            <span className="flex justify-end gap-2">
              {p.status === "pending" && (
                <>
                  <button className="btn h-7!" onClick={() => decide(p.id, true)} title="Approve for the team">
                    <Check size={12} /> Approve
                  </button>
                  <button className="cap hover:text-red-400!" onClick={() => decide(p.id, false)} title="Reject">
                    <X size={12} className="inline" /> reject
                  </button>
                </>
              )}
            </span>
          </div>
        ))}
      </Panel>

      <Panel fig="SHARED" title="Shared tools" right="shared/tools · mounted for every agent with the permissions they need">
        <ToolRows items={shared} open={open} setOpen={setOpen} />
      </Panel>

      <Panel fig="PERSONAL" title="Personal tools" right="agents/<agent>/tools · only their owner uses them">
        <ToolRows items={personal} open={open} setOpen={setOpen} />
      </Panel>
    </div>
  );
}
