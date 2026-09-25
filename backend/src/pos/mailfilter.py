"""A cheap, rule-based prefilter for new-mail events (no model, no tokens).

knowlage ingests all e-mail through its own Gmail connector; the Mail agent
only reacts to mail that may need the owner. Before a gmail event becomes a
task (and so a run), these rules drop bulk and automatic mail: newsletters
(List-Unsubscribe, List-Id, Precedence bulk/list), no-reply and notification
senders, Gmail's Promotions/Social/Updates/Forums categories, and auto-submitted
messages (out-of-office, bounces). Skipped events are still stored (with the
reason) so they are counted and never processed twice.

The rules can be changed without code: POS_MAIL_PREFILTER points to a JSON file
with any of the keys of DEFAULTS (a key replaces the default list), and
POS_MAIL_PREFILTER=off turns the prefilter off.
"""

import json
import os
import re
from pathlib import Path

DEFAULTS = {
    # Any of these headers present: a mailing list or newsletter.
    "list_headers": ["list-unsubscribe", "list-id"],
    # Precedence header values of bulk mail.
    "precedence": ["bulk", "list", "junk"],
    # Sender address patterns (regex, case-insensitive) that never need a reply.
    "senders": [r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|bounces?)[@+.-]",
                r"@(.*\.)?(notifications?|noreply)\."],
    # Gmail category labels.
    "categories": ["CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"],
    # Auto-Submitted values that mean a machine sent it ("no" means a person did).
    "auto_submitted": ["auto-generated", "auto-replied", "auto-notified"],
}


def rules() -> dict | None:
    """The active rules, or None when the prefilter is off."""
    setting = os.environ.get("POS_MAIL_PREFILTER", "")
    if setting.lower() == "off":
        return None
    out = {k: list(v) for k, v in DEFAULTS.items()}
    if setting:
        out.update(json.loads(Path(setting).read_text(encoding="utf-8")))
    return out


def _headers(event: dict) -> dict[str, str]:
    meta = event.get("meta") or {}
    raw = meta.get("headers") or {}
    if isinstance(raw, list):  # Gmail API style: [{"name": ..., "value": ...}]
        raw = {h.get("name", ""): h.get("value", "") for h in raw if isinstance(h, dict)}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def _address(value: str) -> str:
    m = re.search(r"<([^>]+)>", value or "")
    return (m.group(1) if m else value or "").strip().lower()


def skip_reason(event: dict, active: dict | None = None) -> str | None:
    """Why this new-mail event needs no agent, or None if it may."""
    r = rules() if active is None else active
    if r is None or event.get("source") != "gmail":
        return None
    h = _headers(event)
    for name in r.get("list_headers", []):
        if name.lower() in h:
            return f"mailing list ({name})"
    if h.get("precedence", "").strip().lower() in r.get("precedence", []):
        return f"precedence {h['precedence'].strip().lower()}"
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no" and (auto in r.get("auto_submitted", []) or auto.startswith("auto-")):
        return f"auto-submitted ({auto})"
    sender = _address(h.get("from") or event.get("author") or "")
    for pattern in r.get("senders", []):
        if sender and re.search(pattern, sender, re.IGNORECASE):
            return f"automatic sender ({sender})"
    labels = {str(x).upper() for x in (event.get("meta") or {}).get("labels", [])}
    for cat in r.get("categories", []):
        if cat.upper() in labels:
            return f"Gmail category {cat.upper().removeprefix('CATEGORY_').lower()}"
    return None
