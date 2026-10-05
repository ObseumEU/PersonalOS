import { Pencil } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { agentsApi, type Org as OrgData, type OrgMember } from "../agentsApi";
import { ActorChip, Pill, StatusDot, roleSaysMore } from "../components/agents/bits";
import { toast } from "../components/overlay";
import { Panel } from "../components/ui";
import { t } from "../i18n";

const roleLabel = (s: string | null) => (s ? s.replace(/_/g, " ") : "—");

/** Everyone below `id` (a member may not report to one of its own reports). */
function below(id: number, tree: Map<number | null, OrgMember[]>): Set<number> {
  const out = new Set<number>();
  const walk = (x: number) => (tree.get(x) ?? []).forEach((r) => out.has(r.id) || (out.add(r.id), walk(r.id)));
  walk(id);
  return out;
}

function treeOf(members: OrgMember[], moves: Record<number, number> = {}) {
  const ids = new Set(members.map((m) => m.id));
  const tree = new Map<number | null, OrgMember[]>();
  for (const m0 of members) {
    const m = m0.id in moves ? { ...m0, reports_to: moves[m0.id] } : m0;
    const up = m.reports_to != null && ids.has(m.reports_to) ? m.reports_to : null;
    tree.set(up, [...(tree.get(up) ?? []), m]);
  }
  return tree;
}

function Branch({
  m,
  tree,
  depth,
  members,
  editing,
  moved,
  onMove,
}: {
  m: OrgMember;
  tree: Map<number | null, OrgMember[]>;
  depth: number;
  members: OrgMember[];
  editing: boolean;
  moved: boolean;
  onMove: (id: number, to: number) => void;
}) {
  const reports = [...(tree.get(m.id) ?? [])].sort((a, b) => (a.team ?? "").localeCompare(b.team ?? "") || a.name.localeCompare(b.name));
  const blocked = m.is_owner ? null : below(m.id, tree);
  return (
    <>
      <div
        className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line py-2 pr-4 hover:bg-raised ${moved ? "bg-accent/5" : ""}`}
        style={{ paddingLeft: 16 + Math.min(depth, 6) * 20 }}
      >
        <Link to={`/team/${m.id}`} className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          {depth > 0 && <span className="text-ink-2" aria-hidden>└</span>}
          <ActorChip a={m} />
          {roleSaysMore(m.name, roleLabel(m.role)) && <Pill>{roleLabel(m.role)}</Pill>}
          {m.team && <span className="truncate text-xs text-ink-2">{t("org.team", { team: m.team })}</span>}
        </Link>
        {editing && blocked && (
          <label className="flex items-center gap-1.5 text-xs text-ink-2">
            {t("org.reports_to")}
            <select
              aria-label={t("org.reports_to_of", { name: m.name })}
              className="h-7 max-w-[180px] rounded border border-line bg-bg px-1 text-xs text-ink outline-none focus:border-accent"
              value={m.reports_to ?? ""}
              onChange={(e) => e.target.value && onMove(m.id, Number(e.target.value))}
            >
              {members
                .filter((x) => x.id !== m.id && !blocked.has(x.id))
                .map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.is_owner ? t("who.owner") : x.name}
                  </option>
                ))}
            </select>
          </label>
        )}
        <StatusDot status={m.status} />
      </div>
      {reports.map((r) => (
        <Branch key={r.id} m={r} tree={tree} depth={depth + 1} members={members} editing={editing} moved={false} onMove={onMove} />
      ))}
    </>
  );
}

/** The structure as a readable list (the agent network graph is gone). Archived members are not in /api/org. */
export function StructureList({ editable = false, className = "" }: { editable?: boolean; className?: string }) {
  const [data, setData] = useState<OrgData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [moves, setMoves] = useState<Record<number, number>>({});
  const [saving, setSaving] = useState(false);
  const load = useCallback(() => agentsApi.org().then(setData, (e) => setError(e.message)), []);
  useEffect(() => {
    load();
  }, [load]);

  if (!data) return <p className="p-6 text-sm text-ink-2">{error ?? t("act.loading")}</p>;
  const tree = treeOf(data.members, moves);
  const roots = (tree.get(null) ?? []).sort((a, b) => Number(b.is_owner) - Number(a.is_owner));
  const changed = Object.keys(moves).length;

  const save = async () => {
    setSaving(true);
    const before = Object.fromEntries(Object.keys(moves).map((id) => [id, data.members.find((m) => m.id === Number(id))?.reports_to ?? null]));
    try {
      for (const [id, to] of Object.entries(moves)) await agentsApi.setOrg(Number(id), { reports_to: to });
      setMoves({});
      setEditing(false);
      await load();
      toast(t("org.saved", { n: changed }), {
        undo: async () => {
          for (const [id, to] of Object.entries(before)) await agentsApi.setOrg(Number(id), { reports_to: to });
          await load();
          toast(t("org.undone"));
        },
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Panel
      title={t("org.hierarchy")}
      className={className}
      right={
        editable ? (
          editing ? (
            <span className="flex items-center gap-2">
              <button className="btn-accent h-7!" disabled={!changed || saving} onClick={save}>
                {t("act.save")}
                {changed ? ` (${changed})` : ""}
              </button>
              <button
                className="btn h-7!"
                onClick={() => {
                  setMoves({});
                  setEditing(false);
                }}
              >
                {t("act.cancel")}
              </button>
            </span>
          ) : (
            <button className="btn h-7!" onClick={() => setEditing(true)}>
              <Pencil size={13} /> {t("act.edit")}
            </button>
          )
        ) : (
          t("org.members", { n: data.members.length })
        )
      }
    >
      {editing && <p className="border-b border-line px-4 py-2 text-xs text-ink-2">{t("org.edit_hint")}</p>}
      {roots.map((m) => (
        <Branch
          key={m.id}
          m={m}
          tree={tree}
          depth={0}
          members={data.members}
          editing={editing}
          moved={false}
          onMove={(id, to) => setMoves((x) => ({ ...x, [id]: to }))}
        />
      ))}
    </Panel>
  );
}

export default function Org() {
  const [data, setData] = useState<OrgData | null>(null);
  useEffect(() => {
    agentsApi.org().then(setData, () => undefined);
  }, []);
  const roles = new Map<string, number>();
  data?.members.forEach((m) => roles.set(roleLabel(m.role), (roles.get(roleLabel(m.role)) ?? 0) + 1));
  return (
    <div className="flex flex-col gap-5">
      <p className="max-w-3xl text-sm leading-relaxed text-ink-2">{t("org.sub")}</p>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <StructureList editable className="min-w-0 lg:col-span-8" />
        <Panel title={t("org.roles")} right={t("org.per_role")} className="min-w-0 lg:col-span-4">
          {[...roles.entries()].sort().map(([role, n]) => (
            <div key={role} className="flex items-baseline border-b border-line px-4 py-2 text-[13px] last:border-0">
              {role}
              <span className="ml-auto font-mono text-xs text-ink-2">{n}</span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}
