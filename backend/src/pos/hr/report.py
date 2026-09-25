"""Weekly overview for the owner, filed as a "k přečtení" task (spec 4.1)."""

from .interfaces import HRActions
from .metrics import TeamKpis
from .models import AgentRecord
from .review import ProposalKind, ReviewResult

_KIND_LABEL = {
    ProposalKind.ARCHIVE_EXPIRED: "archivován (vypršel)",
    ProposalKind.ARCHIVE_DONE: "archivován (hotovo)",
    ProposalKind.ARCHIVE_IDLE: "archivován (nečinný)",
    ProposalKind.ARCHIVE_INEFFECTIVE: "archivován (neefektivní)",
    ProposalKind.PROMOTE_LONG_LIVED: "změněn na dlouhodobého",
    ProposalKind.MERGE: "navržen ke sloučení",
    ProposalKind.REVISE_INSTRUCTIONS: "instrukce k úpravě",
    ProposalKind.OVER_CAPACITY: "nad limitem",
}


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def weekly_report(
    result: ReviewResult,
    agents: list[AgentRecord],
    previous: TeamKpis | None = None,
) -> str:
    names = {a.id: a.name for a in agents}
    k, p = result.kpis, previous
    lines = [
        f"# HR přehled {result.at:%Y-%m-%d}",
        "",
        f"Aktivních agentů: {result.active_after} (před revizí {result.active_before}).",
        "",
        "| Metrika | Tento týden | Minule |",
        "|---|---|---|",
        f"| Dokončené úkoly | {k.tasks_completed} | {p.tasks_completed if p else '–'} |",
        f"| Bez zásahu majitele | {_pct(k.unassisted_rate)} | {_pct(p.unassisted_rate) if p else '–'} |",
        f"| Vráceno majitelem | {_pct(k.returned_rate)} | {_pct(p.returned_rate) if p else '–'} |",
        f"| Zásahy „musel jsem hlídat“ | {k.owner_interventions} | {p.owner_interventions if p else '–'} |",
        f"| Tokeny | {k.tokens_used:,} | {f'{p.tokens_used:,}' if p else '–'} |".replace(",", " "),
        "",
    ]

    rated = sorted(
        (r for r in result.ratings.values() if r.score is not None),
        key=lambda r: r.score,
        reverse=True,
    )
    if rated:
        lines += ["## Agenti podle efektivity", ""]
        lines += [
            f"- {names.get(r.agent_id, r.agent_id)}: skóre {r.score:.2f}, "
            f"hotovo {r.finished}, vráceno {_pct(r.return_rate)}"
            for r in rated
        ]
        lines.append("")

    if result.proposals:
        lines += ["## Co HR udělal nebo navrhuje", ""]
        for prop in result.proposals:
            who = names.get(prop.agent_id, prop.agent_id) or "tým"
            lines.append(f"- {who}: {_KIND_LABEL[prop.kind]}, {prop.reason}")
        lines.append("")
    else:
        lines += ["Žádné změny nejsou potřeba.", ""]

    return "\n".join(lines).rstrip() + "\n"


def file_weekly_report(
    result: ReviewResult,
    agents: list[AgentRecord],
    actions: HRActions,
    owner_id: str = "owner",
    previous: TeamKpis | None = None,
) -> str:
    """Create the weekly overview as a read-only task for the owner and return its id."""
    return actions.create_task(
        f"HR přehled {result.at:%Y-%m-%d}",
        weekly_report(result, agents, previous),
        assignee=owner_id,
        kind="read",
    )
