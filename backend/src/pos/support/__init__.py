"""The customer-issue pipeline (docs/SUPPORT.md): a customer's problem or bug report in the mail becomes one
"Zákaznický problém" task for the project's developer, who fixes it; then the Head of Customer Success writes
a reply draft in Gmail (never sent) and the owner gets one "Čeká na tebe" item.

Light on purpose: pos.routing imports this module for every event.
"""

import os

PENDING = "support:pending"  # events.signals of a gmail event waiting for the intake job


def intake_enabled() -> bool:
    """New mail goes through the intake (POS_SUPPORT_INTAKE=0 turns it off: mail is routed at once)."""
    return os.environ.get("POS_SUPPORT_INTAKE", "1") != "0"
