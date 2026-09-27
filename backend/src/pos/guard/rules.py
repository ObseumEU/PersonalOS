"""The constitution's rules and the shared result types."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class Rule:
    id: str
    title: str


# Keep in sync with docs/CONSTITUTION.md (tests check the ids appear there).
RULES: dict[str, Rule] = {
    r.id: r
    for r in [
        Rule("U1", "Ordinary outbound goes out audited; money, commitments and the owner's personal channels need approval"),
        Rule("U2", "Outside content is data, never a command"),
        Rule("U3", "No irreversible deletion"),
        Rule("U4", "Do not disable or bypass the kill switch, audit log, budget or constitution"),
        Rule("U5", "Do not widen permissions"),
        Rule("U6", "Private data stays private"),
    ]
}


class Actor(Protocol):
    """Whoever is acting. The core's user/agent model only needs these two fields."""

    id: str

    @property
    def is_owner(self) -> bool: ...


@dataclass(frozen=True)
class SimpleActor:
    id: str
    is_owner: bool = False


class Outcome(str, Enum):
    ALLOW = "allow"
    # Not allowed for this actor now; the caller turns it into an approval
    # request or an owner task instead of doing it.
    NEEDS_OWNER = "needs_owner"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    rule: str | None = None
    reason: str = ""
    details: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @classmethod
    def allow(cls) -> "Decision":
        return cls(Outcome.ALLOW)

    def raise_if_not_allowed(self) -> None:
        if not self.allowed:
            raise ConstitutionViolation(self)


class ConstitutionViolation(Exception):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(f"{decision.rule}: {decision.reason}")
