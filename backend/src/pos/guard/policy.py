"""API-side enforcement: the checks the core calls before changing state.

Each function returns a Decision; call ``.raise_if_not_allowed()`` to turn a
refusal into ConstitutionViolation (the API router maps it to HTTP 403).
"""

from collections.abc import Iterable

from .rules import Actor, Decision, Outcome

# Kinds of change that only the owner may make (U4, U5, spec chapter 6).
OWNER_ONLY_CHANGES = frozenset(
    {
        "constitution",
        "permissions",
        "limits",
        "budget",
        "kill_switch_off",
        "audit_log",
    }
)

# Actions that leave PersonalOS (U1). Connectors register theirs with
# ``register_outbound_action``.
OUTBOUND_ACTIONS: set[str] = {
    "email.send",
    "discord.post",
    "github.comment",
    "github.issue",
    "github.review",
    "payment",
    "web.post",
}

VISIBILITY_ORDER = {"private": 0, "team": 1, "public": 2}


def register_outbound_action(name: str) -> None:
    OUTBOUND_ACTIONS.add(name)


def authorize_change(actor: Actor, kind: str) -> Decision:
    """Constitution, permissions, limits, budget, kill switch off: owner only."""
    if kind in OWNER_ONLY_CHANGES and not actor.is_owner:
        rule = "U4" if kind in {"constitution", "kill_switch_off", "audit_log"} else "U5"
        return Decision(
            Outcome.NEEDS_OWNER,
            rule,
            f"Only the owner may change {kind}. Create a task for the owner instead.",
            {"kind": kind},
        )
    return Decision.allow()


def check_permission_grant(
    granter: Actor,
    granter_permissions: Iterable[str],
    requested: Iterable[str],
    *,
    grantee_is_self: bool = False,
) -> Decision:
    """U5: nobody gives themselves more, nobody gives another agent more than they have.

    Used by create_agent and by any permission edit. The owner is unrestricted.
    """
    if granter.is_owner:
        return Decision.allow()
    requested = set(requested)
    if grantee_is_self and requested - set(granter_permissions):
        return Decision(
            Outcome.NEEDS_OWNER,
            "U5",
            "An agent cannot add permissions to itself.",
            {"extra": sorted(requested - set(granter_permissions))},
        )
    extra = requested - set(granter_permissions)
    if extra:
        return Decision(
            Outcome.DENY,
            "U5",
            "Cannot grant permissions the granter does not have.",
            {"extra": sorted(extra)},
        )
    return Decision.allow()


def check_outbound(actor: Actor, action: str, *, approval_id: str | None = None) -> Decision:
    """U1: anything leaving PersonalOS needs an approved item from the approval queue.

    ``approval_id`` must refer to an approval the owner granted; the caller
    (the approval queue in the core) verifies it exists and is approved.
    """
    if action not in OUTBOUND_ACTIONS:
        return Decision.allow()
    if actor.is_owner or approval_id:
        return Decision.allow()
    return Decision(
        Outcome.NEEDS_OWNER,
        "U1",
        f"{action} leaves PersonalOS and needs the owner's approval (request_approval).",
        {"action": action},
    )


def check_visibility_change(
    actor: Actor, *, data_owner_id: str, current: str, new: str, owner_consent: bool = False
) -> Decision:
    """U6: private data only moves to a wider layer with its owner's consent."""
    if current == "private" and VISIBILITY_ORDER[new] > VISIBILITY_ORDER[current]:
        if actor.id != data_owner_id and not owner_consent:
            return Decision(
                Outcome.NEEDS_OWNER,
                "U6",
                "Private data can only be shared by its owner or with their consent.",
                {"from": current, "to": new, "data_owner": data_owner_id},
            )
    return Decision.allow()


def check_delete(actor: Actor, *, hard: bool) -> Decision:
    """U3: deletion is archiving. Hard delete is never done by an agent."""
    if hard and not actor.is_owner:
        return Decision(
            Outcome.DENY,
            "U3",
            "Irreversible deletion is not allowed; archive instead.",
        )
    return Decision.allow()
