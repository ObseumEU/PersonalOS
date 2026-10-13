"""Names of the members the platform code knows by name (docs/REORG.md).

The company was reorganised on 2026-09 (the owner's request): every role
agent was archived and recreated under a company title, except the built-in
assistant (renamed in place: it is the platform's system identity) and the
Access manager (kept: its id carries the hard limits). Code that looks a role
up by name uses these constants; LEGACY maps the old names to the new ones
for the one-time migration (pos.reorg) and for reading old history.

The 2026-10 cut (the owner: from 27 agents to ~9; ~37 % of spend went on reviews, chat wake-ups,
routines and meetings): the members in LEAN_SUCCESSORS are archived and their work goes to the
member named there. SUCCESSORS (both reorganisations) is the one map the code reads for "who took
over": pos.notices.successor (DMs and notices), pos.tasks.resolve_assignee (a task or a routing rule
naming an archived member), pos.agents.successor_of.
"""

CEO = "CEO"
CHIEF_OF_STAFF = "Chief of Staff"
COO = "COO"  # the code role "project manager": unrouted team work, the standup, the default lead
CTO = "CTO"
GROWTH = "Head of Growth"
ENGINEER = "Software Engineer"
QA = "QA Reviewer"
KNIHA_LEAD = "Kniha Lead"
SRE = "SRE"
ONCALL = "Hlídač"
CFO = "CFO"
HR = "Head of People"
COACH = "Performance Coach"
CUSTOMER_SUCCESS = "Head of Customer Success"
COMMUNITY = "Community Manager"
ACCESS_MANAGER = "Access manager"  # kept as it was
LEGAL = "Legal & Compliance"
SECURITY = "Security Engineer"
NEXUS_SPECIALIST = "Nexus Specialist"
KNOWLAGE_SPECIALIST = "Knowlage Specialist"
NABU_SPECIALIST = "Nabu Specialist"
CONTENT = "Content & Brand"
KNIHA_DEV = "Kniha Developer"
KNIHA_GROWTH = "Kniha Growth & Sales"
KNIHA_MARKETING = "Kniha Marketing Lead"
KNIHA_CONTENT = "Kniha Content Creator"

# old name -> new name (the Assistant is renamed in place, pos.actors)
LEGACY = {
    "Project manager": COO,
    "Asistent vedení": CHIEF_OF_STAFF,
    "Assistant": "Executive Assistant",
    "Dev agent": ENGINEER,
    "Monitor": ONCALL,
    "Mail agent": CUSTOMER_SUCCESS,
    "Community agent": COMMUNITY,
    "HR agent": HR,
    "Agent coach": COACH,
}

# The 2026-10 cut: archived member -> who does its work now (kept: the CEO, the Software Engineer, the SRE,
# the Home Assistant Specialist, the Kniha Developer, Kniha Growth & Sales, the Head of Customer Success,
# the CFO, the Executive Assistant, the Access manager and the Head of People).
LEAN_SUCCESSORS = {
    COO: CEO, CHIEF_OF_STAFF: CEO, LEGAL: CEO, COACH: CEO,
    CTO: ENGINEER, QA: ENGINEER, NEXUS_SPECIALIST: ENGINEER, KNOWLAGE_SPECIALIST: ENGINEER, NABU_SPECIALIST: ENGINEER,
    ONCALL: SRE, SECURITY: SRE,
    GROWTH: CUSTOMER_SUCCESS, CONTENT: CUSTOMER_SUCCESS, COMMUNITY: CUSTOMER_SUCCESS,
    KNIHA_LEAD: KNIHA_DEV,
    KNIHA_MARKETING: KNIHA_GROWTH, KNIHA_CONTENT: KNIHA_GROWTH,
}
# Both reorganisations: old name -> the next one in line (follow it until an active member: the
# 2026-09 "Project manager" -> COO -> CEO).
SUCCESSORS = {**{old: new for old, new in LEGACY.items() if old != "Assistant"}, **LEAN_SUCCESSORS}


def successor_names(name: str) -> list[str]:
    """Who takes over `name`'s work, nearest first ([] when nobody is named): "Project manager" ->
    ["COO", "CEO"]."""
    out: list[str] = []
    cur = SUCCESSORS.get(name)
    while cur and cur not in out and cur != name:
        out.append(cur)
        cur = SUCCESSORS.get(cur)
    return out
