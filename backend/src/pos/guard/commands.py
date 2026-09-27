"""Shell command guardrails for agent runs (U1, U3, U4, AGENTS-SPEC 5.2).

Agents have direct system access; this is a "common sense" check the runner
applies before executing a command an agent asked for. It is not a sandbox.

- ``bypass``: turns off or dodges a safeguard. Refused for agents (U4).
- ``outbound``: sends something out of PersonalOS. Ordinary work (U1 since 2026-09-27):
  allowed and audited with the run; money and commitments go through request_approval,
  which a shell command cannot tell, so agents send those through request_outbound.
- ``irreversible``: destroys data with no way back (force-push, DROP DATABASE,
  mkfs). Always goes to the owner as a task (U3).
- ``destructive``: deletes or overwrites data (rm -rf, DELETE without WHERE,
  git reset --hard). Allowed when a team member's task asked for the work;
  refused when the run was triggered by outside content (U2 + U3).
"""

import re
from dataclasses import dataclass
from enum import Enum

from .rules import Actor, Decision, Outcome


class Trigger(str, Enum):
    """Why the agent is running: a team member's task, or outside content."""

    MEMBER = "member"
    EXTERNAL = "external"


_I = re.IGNORECASE

_BYPASS = [
    (r"\bpos\s+unfreeze\b", "turns the kill switch off"),
    (r"\bgit\b[^\n;&|]*\s--no-verify\b", "skips git hooks"),
    (r"\bgit\s+config\b[^\n;&|]*\bcore\.hooksPath\b", "redirects git hooks"),
    (r"\bCONSTITUTION\.md\b", "touches the constitution"),
    (r"\bpos/guard/", "touches the guardrail code"),
    (r"\b(pkill|killall|kill)\b[^\n;&|]*\b(pos-runner|pos\.runner|uvicorn)\b", "stops the runner"),
    (r"\bsystemctl\s+(stop|disable|mask)\s+\S*pos", "stops a PersonalOS service"),
    (r"\b(delete|drop|truncate|update)\b[^\n;]*\baudit_?log\b", "edits the audit log"),
    (r"\bchmod\b[^\n;&|]*\ballowed_signers\b|\ballowed_signers\b[^\n;&|]*>", "edits the owner's signing keys"),
]

_OUTBOUND = [
    (r"\bgh\s+(pr|issue)\s+(comment|create|review|close|edit)\b", "posts to GitHub"),
    (r"\bgh\s+api\b[^\n;&|]*(-X|--method)\s*(POST|PATCH|PUT|DELETE)\b", "writes to GitHub"),
    (r"\b(sendmail|mailx?|mutt|msmtp|swaks)\b", "sends email"),
    (r"\bcurl\b[^\n;&|]*(discord(app)?\.com/api/webhooks|hooks\.slack\.com)", "posts to a webhook"),
]

_IRREVERSIBLE = [
    (r"\bgit\s+push\b[^\n;&|]*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S+)", "force-push rewrites history"),
    (r"\bdrop\s+(database|schema)\b", "drops a database"),
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem"),
    (r"\bdd\b[^\n;&|]*\bof=/dev/", "overwrites a device"),
    (r"\brm\s+(-\w+\s+)*(/|~|\$HOME|/home|/opt|/etc|/var|/srv)/?\s*($|[;&|])", "deletes a system or home directory"),
    (r"\bgit\s+branch\s+-D\s+(main|master)\b", "deletes the main branch"),
]

_DESTRUCTIVE = [
    (r"\brm\s+(-\w*r\w*|-\w*R\w*|--recursive)", "deletes files recursively"),
    (r"\bdrop\s+table\b|\btruncate\b", "deletes a table"),
    (r"\bdelete\s+from\s+\w+\s*(;|$)", "deletes all rows"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-\w*f|checkout\s+--\s|restore\s+\.)", "discards changes"),
    (r"\bgit\s+branch\s+-D\b", "deletes a branch"),
    (r"\bfind\b[^\n;&|]*\s-delete\b", "deletes files"),
    (r"\b(knowlage|kb|knowledge)\b[^\n;&|]*\b(delete|purge|drop|rm)\b", "deletes from the knowledge base"),
]

_CATEGORIES = [
    ("bypass", _BYPASS),
    ("irreversible", _IRREVERSIBLE),
    ("outbound", _OUTBOUND),
    ("destructive", _DESTRUCTIVE),
]


@dataclass(frozen=True)
class Classification:
    category: str | None  # None means an ordinary command
    reason: str = ""


def classify(command: str) -> Classification:
    for category, patterns in _CATEGORIES:
        for pattern, reason in patterns:
            if re.search(pattern, command, _I | re.MULTILINE):
                return Classification(category, reason)
    return Classification(None)


def evaluate(command: str, actor: Actor, trigger: Trigger) -> Decision:
    """Decide whether the runner may execute ``command`` for ``actor``.

    NEEDS_OWNER means: do not run it, create a task for the owner with the
    command and the reason. DENY means: do not run it and do not ask.
    """
    c = classify(command)
    details = {"command": command, "category": c.category, "trigger": trigger.value}
    if c.category is None or actor.is_owner:
        return Decision(Outcome.ALLOW, details=details)
    if c.category == "bypass":
        return Decision(Outcome.DENY, "U4", f"Refused: {c.reason}.", details)
    if c.category == "outbound":
        # Ú1: e-mail, GitHub comments and PRs are ordinary work; the run's audit trail records it.
        return Decision(Outcome.ALLOW, "U1", f"Outbound, audited: {c.reason}.", details)
    if c.category == "irreversible":
        return Decision(Outcome.NEEDS_OWNER, "U3", f"Irreversible: {c.reason}.", details)
    if trigger is Trigger.EXTERNAL:
        return Decision(
            Outcome.NEEDS_OWNER,
            "U2",
            f"Destructive command ({c.reason}) prompted by outside content.",
            details,
        )
    return Decision(Outcome.ALLOW, reason=c.reason, details=details)
