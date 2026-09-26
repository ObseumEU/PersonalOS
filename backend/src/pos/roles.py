"""Names of the members the platform code knows by name (docs/REORG.md).

The company was reorganised on 2026-09 (the owner's request): every role
agent was archived and recreated under a company title, except the built-in
assistant (renamed in place: it is the platform's system identity) and the
Access manager (kept: its id carries the hard limits). Code that looks a role
up by name uses these constants; LEGACY maps the old names to the new ones
for the one-time migration (pos.reorg) and for reading old history.
"""

CEO = "CEO"
CHIEF_OF_STAFF = "Chief of Staff"
COO = "COO"  # the code role "project manager": unrouted team work, the standup, the default lead
CTO = "CTO"
ENGINEER = "Software Engineer"
QA = "QA Reviewer"
SRE = "SRE"
ONCALL = "Hlídač"
CFO = "CFO"
HR = "Head of People"
COACH = "Performance Coach"
CUSTOMER_SUCCESS = "Head of Customer Success"
COMMUNITY = "Community Manager"
ACCESS_MANAGER = "Access manager"  # kept as it was

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
