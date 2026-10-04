"""API-side enforcement: the checks the core calls before changing state.

Each function returns a Decision; call ``.raise_if_not_allowed()`` to turn a
refusal into ConstitutionViolation (the API router maps it to HTTP 403).
"""

import re
from collections.abc import Iterable
from urllib.parse import urlparse

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
    "github.pr",
    "linkedin.post",
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


# ------------------------------------------------------------------ U1: what waits for approval
#
# The constitution of 2026-09-27 (Ú1): ordinary outbound work (e-mail, customer replies,
# Discord, GitHub comments, issues and pull requests) goes out without approval; every send
# is audited and the CEO reviews them daily. Only these three kinds wait for the owner.
APPROVAL_KINDS: dict[str, str] = {
    "money": "a payment, purchase or anything that costs money outside the approved budgets",
    "commitment": "a contract, price quote or other legal or financial commitment",
    "personal_channel": "a post on the owner's personal channels (LinkedIn, personal social networks)",
}
ORDINARY = "ordinary"
OUTBOUND_KINDS = (ORDINARY, *APPROVAL_KINDS)

# Actions that are money by what they are, whatever the payload says.
MONEY_ACTIONS = frozenset({"payment"})
# Actions that are the owner's personal channel by what they are (his LinkedIn profile).
PERSONAL_CHANNEL_ACTIONS = frozenset({"linkedin.post"})
# The owner's personal channels: posting there is always his call.
PERSONAL_CHANNEL_HOSTS = (
    "linkedin.com", "lnkd.in", "facebook.com", "fb.com", "instagram.com", "x.com", "twitter.com", "threads.net",
    "tiktok.com", "bsky.app", "mastodon.social", "medium.com", "substack.com",
)
PERSONAL_CHANNEL_WORDS = re.compile(
    r"\b(linkedin|facebook|instagram|twitter|tiktok|threads|bluesky|mastodon|owner'?s? personal|osobní (?:profil|síť|sítě|kanál))\b",
    re.IGNORECASE)
# Cheap content rules. They only ever move a send *toward* approval; a miss is audited and
# the CEO sees it in the daily digest.
_MONEY_TEXT = re.compile(
    r"\b(we(?:'ll| will)? (?:buy|purchase|pay for|order)|i(?:'ll| will)? (?:buy|purchase|pay for)|purchase order|"
    r"wire transfer|bank transfer|please charge|charge (?:our|my) card|subscribe (?:us|me) to|"
    r"zaplatíme|uhradíme|koupíme|objednáváme|závazně objednáv\w*|převedeme|pošleme platbu|předplatíme|"
    r"ads? budget|boost(?:ed)? post|sponsored post|placen\w* reklam\w*|sponzorovan\w* příspěv\w*|"
    r"rozpočet na reklam\w*)\b",
    re.IGNORECASE)
_COMMITMENT_TEXT = re.compile(
    r"\b(price quote|quotation|our (?:offer|quote|pricing proposal)|binding offer|contract|agreement|"
    r"terms and conditions|NDA|SLA|we (?:hereby )?(?:agree to|commit to|guarantee)|"
    r"sign(?:ing)? (?:the|this) (?:contract|agreement|order|offer)|"
    r"cenov\w* nabídk\w*|nabídk\w* ceny|závazn\w* nabídk\w*|smlouv\w*|dohod\w* o|zavazujeme se|garantujeme|"
    r"podepíš\w*|podepíšeme|podeps\w*|obchodní podmínky)\b",
    re.IGNORECASE)
_AMOUNT = re.compile(r"(\d[\d\s.,]*\s?(?:kč|czk|eur|€|usd|\$)|(?:€|\$)\s?\d)", re.IGNORECASE)
_PRICE_WORDS = re.compile(r"\b(price|pricing|cost|costs|fee|discount|total|cena|ceny|stojí|poplatek|sleva|celkem)\b",
                          re.IGNORECASE)


def _host(url: str) -> str:
    return (urlparse(url if "//" in url else "//" + url).hostname or "").lower()


def _text_of(payload: dict) -> str:
    return " ".join(str(payload.get(k) or "") for k in ("subject", "title", "body", "content", "reason"))


def classify_outbound(action: str, payload: dict | None = None, kind: str | None = None) -> tuple[str, str]:
    """(kind, reason) for one outbound send: 'ordinary' goes out now; money, commitment
    and personal_channel wait for the owner's approval.

    ``kind`` is the sender's own label. It can move a send toward approval, never away:
    a rule that sees money, a commitment or a personal channel wins over 'ordinary'."""
    payload = payload or {}
    if kind is not None and kind not in OUTBOUND_KINDS:
        raise ValueError(f"kind must be one of {OUTBOUND_KINDS}")
    if kind in APPROVAL_KINDS:
        return kind, f"marked as {kind}: {APPROVAL_KINDS[kind]}"
    if action in MONEY_ACTIONS:
        return "money", f"{action} moves money"
    if action in PERSONAL_CHANNEL_ACTIONS:
        return "personal_channel", f"{action} posts on the owner's personal channel"
    target = str(payload.get("url") or payload.get("channel") or payload.get("platform") or "")
    host = _host(target) if target else ""
    if host and any(host == h or host.endswith("." + h) for h in PERSONAL_CHANNEL_HOSTS):
        return "personal_channel", f"{host} is one of the owner's personal channels"
    if target and not host.count(".") and PERSONAL_CHANNEL_WORDS.search(target):
        return "personal_channel", f"'{target[:40]}' is one of the owner's personal channels"
    text = _text_of(payload)
    m = _MONEY_TEXT.search(text)
    if m:
        return "money", f"'{m.group(0)}' reads as buying or paying"
    m = _COMMITMENT_TEXT.search(text)
    if m:
        return "commitment", f"'{m.group(0)}' reads as a contract or a binding offer"
    if _AMOUNT.search(text) and _PRICE_WORDS.search(text):
        return "commitment", "a price with an amount reads as a price quote"
    return ORDINARY, "ordinary outbound work (Ú1): sent and audited, the CEO reviews daily"


def needs_approval(action: str, payload: dict | None = None, kind: str | None = None) -> bool:
    return classify_outbound(action, payload, kind)[0] != ORDINARY


def check_outbound(actor: Actor, action: str, *, approval_id: str | None = None,
                   payload: dict | None = None, kind: str | None = None) -> Decision:
    """U1: ordinary outbound goes out (audited); money, commitments and the owner's personal
    channels need an approved item from the approval queue.

    ``approval_id`` must refer to an approval the owner granted; the caller
    (the approval queue in the core) verifies it exists and is approved.
    """
    if action not in OUTBOUND_ACTIONS:
        return Decision.allow()
    if actor.is_owner or approval_id:
        return Decision.allow()
    found, why = classify_outbound(action, payload, kind)
    if found == ORDINARY:
        return Decision(Outcome.ALLOW, "U1", why, {"action": action, "kind": found})
    return Decision(
        Outcome.NEEDS_OWNER,
        "U1",
        f"{action} is {APPROVAL_KINDS[found]} ({why}) and needs the owner's approval (request_approval).",
        {"action": action, "kind": found},
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
