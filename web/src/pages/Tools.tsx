import { Check, X } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { toast } from "../components/overlay";
import { PageHeader, Panel } from "../components/ui";
import { ago, label, plural, t } from "../i18n";

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

const GRID = "grid grid-cols-[minmax(0,1.6fr)_80px_minmax(0,0.9fr)_70px_100px_minmax(0,1fr)] items-center gap-3";
const PUB_GRID = "grid grid-cols-[50px_minmax(0,1fr)_minmax(0,0.8fr)_70px_110px_minmax(0,1.2fr)_190px] gap-3";

const where = (f: Finding) => `${f.file}${f.line ? `:${f.line}` : ""}`;
const findings = (n: number) => `${n} ${plural(n, "nález", "nálezy", "nálezů")}`;

function ReviewCell({ tool }: { tool: Tool }) {
  if (tool.blocked) return <span className="text-xs text-red-400">{t("tools.blocked")}</span>;
  if (!tool.review.ok)
    return (
      <span className="truncate text-xs text-red-400" title={tool.review.findings.map((f) => `${f.kind}: ${where(f)} ${f.detail}`).join("\n")}>
        {findings(tool.review.findings.length)}
      </span>
    );
  return <span className="text-xs text-accent">{tool.outbound ? t("tools.clean_outbound") : t("tools.clean")}</span>;
}

function Meta({ k, children }: { k: string; children: ReactNode }) {
  return (
    <span className="min-w-0 break-words">
      <span className="text-ink-2">{t(k)}:</span> {children}
    </span>
  );
}

function ToolRows({ items, open, setOpen }: { items: Tool[]; open: string | null; setOpen: (id: string | null) => void }) {
  return (
    <div className="overflow-x-auto">
      <div className="min-w-[640px]">
        <div className={`${GRID} border-b border-line px-4 py-2 text-xs text-ink-2`}>
          <span>{t("tools.h.tool")}</span>
          <span>{t("tools.h.kind")}</span>
          <span>{t("tools.h.owner")}</span>
          <span>{t("tools.h.version")}</span>
          <span>{t("tools.h.uses")}</span>
          <span>{t("tools.h.review")}</span>
        </div>
        {items.length === 0 && <p className="p-4 text-xs text-ink-2">{t("tools.none")}</p>}
        {items.map((tool) => (
          <div key={tool.id} className="border-b border-line">
            <button
              className={`${GRID} w-full px-4 py-2.5 text-left text-[13px] hover:bg-raised`}
              onClick={() => setOpen(open === tool.id ? null : tool.id)}
              aria-expanded={open === tool.id}
            >
              <span className="flex min-w-0 flex-col">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="truncate">{tool.name}</span>
                  {!tool.valid && <span className="shrink-0 rounded border border-amber-400/60 px-1.5 text-xs text-amber-300">{t("tools.invalid")}</span>}
                </span>
                <span className="truncate text-xs text-ink-2">{tool.description}</span>
              </span>
              <span className="text-xs text-ink-2">{tool.kind ? label("tools.kind", tool.kind) : "?"}</span>
              <span className="truncate">{tool.owner}</span>
              <span className="font-mono text-xs">{tool.version}</span>
              <span className="text-xs text-ink-2" title={tool.usage.last_at ? t("tools.last_used", { when: ago(tool.usage.last_at) }) : t("tools.never_used")}>
                {tool.usage.uses}
                {tool.usage.failed ? t("tools.failed", { n: tool.usage.failed }) : ""}
              </span>
              <ReviewCell tool={tool} />
            </button>
            {open === tool.id && (
              <div className="flex flex-col gap-1.5 bg-raised/40 px-4 py-3 text-xs text-ink">
                <Meta k="tools.path">
                  <span className="font-mono">
                    {tool.path}/{tool.entry}
                  </span>
                </Meta>
                <Meta k="tools.tests">
                  <span className="font-mono">{tool.tests}</span>
                </Meta>
                <span className="flex flex-wrap gap-x-3">
                  <Meta k="tools.permissions">{tool.permissions_needed.length ? tool.permissions_needed.map((p) => label("perm", p)).join(", ") : t("tools.none_word")}</Meta>
                  <Meta k="tools.outbound">{tool.outbound ? t("tools.outbound_yes") : t("tools.outbound_no")}</Meta>
                </span>
                {tool.usage.uses > 0 && (
                  <Meta k="tools.usage">
                    {t("tools.usage_line", { ok: tool.usage.ok, uses: tool.usage.uses, users: tool.usage.users, last: ago(tool.usage.last_at) })}
                  </Meta>
                )}
                {tool.publication && (
                  <Meta k="tools.last_pub">
                    #{tool.publication.id} v{tool.publication.version} {label("tools.status", tool.publication.status)}
                  </Meta>
                )}
                {tool.review.findings.map((f, i) => (
                  <span key={i} className="break-words text-red-400">
                    {f.kind}: <span className="font-mono">{where(f)}</span> {f.detail}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
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
    const h = setInterval(load, 30000);
    return () => clearInterval(h);
  }, [load]);
  const decide = (p: Publication, approve: boolean) =>
    api(`/api/tools/publications/${p.id}/decide`, { method: "POST", body: JSON.stringify({ approve }) }).then(
      () => {
        setError(null);
        toast(t(approve ? "tools.approved_toast" : "tools.rejected_toast", { tool: p.tool }));
        load();
      },
      (e) => setError(e.message),
    );

  const shared = lib?.tools.filter((x) => x.scope === "shared") ?? [];
  const personal = lib?.tools.filter((x) => x.scope !== "shared") ?? [];
  const pending = lib?.publications.filter((p) => p.status === "pending").length ?? 0;

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader kicker={t("settings.kicker")} title={t("nav.tools")} sub={t("tools.sub")} />
      {error && <p className="text-xs break-words text-red-400">{error}</p>}

      <Panel title={t("tools.queue")} right={pending ? t("tools.queue_waiting", { n: pending }) : t("tools.queue_empty_right")}>
        {lib?.publications.length === 0 && <p className="p-4 text-xs text-ink-2">{t("tools.queue_empty")}</p>}
        {!!lib?.publications.length && (
          <div className="overflow-x-auto">
            <div className="min-w-[760px]">
              <div className={`${PUB_GRID} border-b border-line px-4 py-2 text-xs text-ink-2`}>
                <span>#</span>
                <span>{t("tools.h.tool")}</span>
                <span>{t("tools.h.from")}</span>
                <span>{t("tools.h.version")}</span>
                <span>{t("tools.h.status")}</span>
                <span>{t("tools.h.guard")}</span>
                <span />
              </div>
              {lib.publications.map((p) => (
                <div key={p.id} className={`${PUB_GRID} items-center border-b border-line px-4 py-2.5 text-[13px]`}>
                  <span className="text-xs text-ink-2">{p.id}</span>
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{p.tool}</span>
                    <span className="text-xs text-ink-2">{ago(p.created_at)}</span>
                  </span>
                  <span className="truncate">{p.from_name ?? "?"}</span>
                  <span className="font-mono text-xs">{p.version}</span>
                  <span className={`text-xs ${p.status === "rejected" ? "text-red-400" : p.status === "pending" ? "text-amber-300" : "text-accent"}`}>
                    {label("tools.status", p.status)}
                  </span>
                  <span className={`truncate text-xs ${p.findings.length ? "text-red-400" : "text-ink-2"}`} title={p.findings.map((f) => `${f.kind}: ${where(f)} ${f.detail}`).join("\n")}>
                    {p.findings.length ? p.findings.map((f) => `${f.kind} (${where(f)})`).join(", ") : t("tools.clean")}
                  </span>
                  <span className="flex justify-end gap-2">
                    {p.status === "pending" && (
                      <>
                        <button className="btn h-7!" onClick={() => decide(p, true)} title={t("tools.approve_title")}>
                          <Check size={12} /> {t("act.approve")}
                        </button>
                        <button className="btn h-7! hover:text-red-400!" onClick={() => decide(p, false)}>
                          <X size={12} /> {t("act.reject")}
                        </button>
                      </>
                    )}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Panel>

      <Panel title={t("tools.shared")} right={t("tools.shared_right")}>
        <ToolRows items={shared} open={open} setOpen={setOpen} />
      </Panel>

      <Panel title={t("tools.personal")} right={t("tools.personal_right")}>
        <ToolRows items={personal} open={open} setOpen={setOpen} />
      </Panel>
    </div>
  );
}
