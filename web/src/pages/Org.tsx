import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { agentsApi, type Org as OrgData, type OrgMember } from "../agentsApi";
import { ActorChip, Pill, StatusDot } from "../components/agents/bits";
import { PageHeader, Panel } from "../components/ui";

const label = (s: string | null) => (s ? s.replace(/_/g, " ") : "—");

/** One member and, under it, everyone who reports to them (grouped by team). */
/** Everyone below `id` (a member may not report to one of its own reports). */
function below(id: number, tree: Map<number | null, OrgMember[]>): Set<number> {
  const out = new Set<number>();
  const walk = (x: number) => (tree.get(x) ?? []).forEach((r) => out.has(r.id) || (out.add(r.id), walk(r.id)));
  walk(id);
  return out;
}

function Branch({
  m,
  tree,
  depth,
  members,
  onMove,
}: {
  m: OrgMember;
  tree: Map<number | null, OrgMember[]>;
  depth: number;
  members: OrgMember[];
  onMove: (id: number, to: number) => void;
}) {
  const reports = [...(tree.get(m.id) ?? [])].sort((a, b) => (a.team ?? "").localeCompare(b.team ?? "") || a.name.localeCompare(b.name));
  const blocked = m.is_owner ? null : below(m.id, tree);
  return (
    <>
      <div
        className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 border-b border-line py-2 pr-4 hover:bg-raised"
        style={{ paddingLeft: 16 + depth * 28 }}
      >
        <Link to={`/agents/${m.id}`} className="flex min-w-0 items-center gap-2">
          {depth > 0 && <span className="cap text-ink-3">└</span>}
          <ActorChip a={m} />
          <Pill>{label(m.role)}</Pill>
          {m.team && <span className="cap truncate">team {m.team}</span>}
        </Link>
        {blocked ? (
          <select
            aria-label={`${m.name} reports to`}
            title="Reports to (a lead moves members only within its own part of the chart)"
            className="h-[22px] max-w-[150px] rounded-[3px] border border-line bg-bg px-1 font-mono text-[11px] text-ink-2 outline-none focus:border-accent"
            value={m.reports_to ?? ""}
            onChange={(e) => e.target.value && onMove(m.id, Number(e.target.value))}
          >
            {members
              .filter((x) => x.id !== m.id && !blocked.has(x.id))
              .map((x) => (
                <option key={x.id} value={x.id}>
                  ↑ {x.is_owner ? "Owner" : x.name}
                </option>
              ))}
          </select>
        ) : (
          <span />
        )}
        <StatusDot status={m.status} />
      </div>
      {reports.map((r) => (
        <Branch key={r.id} m={r} tree={tree} depth={depth + 1} members={members} onMove={onMove} />
      ))}
    </>
  );
}

export default function Org() {
  const [data, setData] = useState<OrgData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    agentsApi.org().then(setData, (e) => setError(e.message));
  }, []);

  if (!data) return <p className="cap p-6">{error ?? "loading…"}</p>;
  const ids = new Set(data.members.map((m) => m.id));
  const tree = new Map<number | null, OrgMember[]>();
  for (const m of data.members) {
    const up = m.reports_to != null && ids.has(m.reports_to) ? m.reports_to : null;
    tree.set(up, [...(tree.get(up) ?? []), m]);
  }
  const roots = (tree.get(null) ?? []).sort((a, b) => Number(b.is_owner) - Number(a.is_owner));
  const roles = new Map<string, number>();
  data.members.forEach((m) => roles.set(label(m.role), (roles.get(label(m.role)) ?? 0) + 1));

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="PLATFORM · ORG"
        title="Who does what"
        sub="Everyone reports to the Project manager, who reports to you. The PM splits team work into steps and assigns them by role; agents hand work to each other and ask peers directly. Change a member's role, team or manager on its page."
      />
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/network?view=org" className="btn">
          Org chart in 3D →
        </Link>
        <Link to="/agents" className="btn">
          Agents →
        </Link>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel fig="FIG. 7" title="Hierarchy" right={`${data.members.length} members`} className="lg:col-span-8">
          {roots.map((m) => (
            <Branch
              key={m.id}
              m={m}
              tree={tree}
              depth={0}
              members={data.members}
              onMove={(id, to) =>
                agentsApi.setOrg(id, { reports_to: to }).then(
                  () => agentsApi.org().then(setData),
                  (e) => setError(e.message),
                )
              }
            />
          ))}
        </Panel>
        <Panel fig="TAB. 13" title="Roles" right="members per role" className="lg:col-span-4">
          {[...roles.entries()].sort().map(([role, n]) => (
            <div key={role} className="flex items-baseline border-b border-line px-4 py-2 text-[13px] last:border-0">
              {role}
              <span className="cap ml-auto">{n}</span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}
