"""Kniha (Rodinné příběhy) reservations without personal data: kniha_reservations_summary.

The Kniha web appends every reservation to a JSONL file on svr03 (volume rodinne-pribehy-reservations,
/app/data/reservations.jsonl in its container). No agent could read it (2026-10 audit): the summary
script (provoz/rezervace-souhrn.mjs in the Kniha repo) runs only where the file lies. The api mounts the
volume read-only (deploy/prod/docker-compose.prod.yml, POS_KNIHA_RESERVATIONS) and this tool counts it
by the script's rules: by day, kind, ref code (src), UTM and the web's section (source), plus unique
contacts. E-mails are used only in memory to count unique contacts; recipient and referral are free text
(names), so only "filled in" counts leave; a source or UTM value that looks personal is hidden. The raw
file never reaches an agent (it is not mounted into the agent pool).

Who: the Kniha team and the CEO (grants tool:kniha_reservations_summary in their agent.json; the group
is RESTRICTED in pos.access, so the default autonomy grants do not hand it to everyone).
"""

import json
import os
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

PERMISSION = "kniha:reservations"
DEFAULT_PATH = "/mnt/kniha-reservations/reservations.jsonl"
KNOWN_SRC = {"owner", *(f"p{i:02d}" for i in range(1, 11))}


def path() -> Path:
    return Path(os.environ.get("POS_KNIHA_RESERVATIONS") or DEFAULT_PATH)


def safe_value(s) -> str:
    """A section/UTM value as it is, unless it may be personal (an @, a long number, spaces, long text)."""
    s = str(s or "").strip()
    if not s:
        return "(prázdný)"
    if re.search(r"@|\d{6,}|\s", s) or len(s) > 40:
        return "(jiný/volný text)"
    return s.lower()


def normalize_src(value) -> str:
    """The web's classifySrc rules: "other:p-01" and "wo-01" (before the alias, T-711) count as p01 / wo01."""
    if not isinstance(value, str) or not value.strip():
        return "(bez src)"
    raw = re.sub(r"^([a-z]+)-(\d+)$", r"\1\2", re.sub(r"^other:", "", value.strip().lower()))
    if not re.fullmatch(r"[a-z0-9-]{1,40}", raw):
        return "(neplatný)"
    return raw if raw in KNOWN_SRC or re.fullmatch(r"wo\d{1,3}", raw) else f"other:{raw}"


def summary(since: str = "", until: str = "", file: Path | None = None) -> dict:
    """Counts of the reservations (test ones left out), optionally from/until a day (YYYY-MM-DD)."""
    for name, value in (("since", since), ("until", until)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as e:
                raise ValueError(f"{name} must be a date YYYY-MM-DD") from e
    f = file or path()
    if not f.exists():
        return {"available": False, "total": 0,
                "note": ("Soubor s rezervacemi zatím neexistuje (žádná rezervace od nasazení), nebo není "
                         "připojený do PersonalOS (POS_KNIHA_RESERVATIONS).")}
    by_day, by_kind, by_source, by_day_src = Counter(), Counter(), Counter(), Counter()
    utm = {k: Counter() for k in ("utm_source", "utm_medium", "utm_campaign")}
    by_src: dict[str, dict] = {}
    emails: set[str] = set()
    broken = tests = outside = recipient = referral = 0
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError
        except ValueError:
            broken += 1
            continue
        if str(r.get("source") or "").lower().startswith("test"):
            tests += 1
            continue
        day = str(r.get("createdAt") or "")[:10] or "(bez data)"
        if (since and not day >= since) or (until and not day <= until):
            outside += 1
            continue
        src = normalize_src(r.get("src"))
        by_day[day] += 1
        by_kind[str(r.get("kind") or "(neznámý)")] += 1
        by_source[safe_value(r.get("source"))] += 1
        by_day_src[(day, src)] += 1
        for k, c in utm.items():
            if r.get(k):
                c[safe_value(r.get(k))] += 1
        s = by_src.setdefault(src, {"src": src, "count": 0, "first": day, "last": day})
        s["count"] += 1
        s["first"], s["last"] = min(s["first"], day), max(s["last"], day)
        if r.get("email"):
            emails.add(str(r["email"]).strip().lower())
        recipient += bool(r.get("recipient"))
        referral += bool(r.get("referral"))
    return {
        "available": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        **({"since": since} if since else {}), **({"until": until} if until else {}),
        "total": sum(by_day.values()), "tests_left_out": tests, "unreadable_lines": broken,
        "outside_range": outside, "unique_contacts": len(emails),
        "gift_recipient_filled": recipient, "referral_filled": referral,
        "by_src": sorted(by_src.values(), key=lambda s: (-s["count"], s["src"])),
        "by_day": dict(sorted(by_day.items())),
        "by_day_src": [{"day": d, "src": s, "count": n} for (d, s), n in sorted(by_day_src.items())],
        "by_kind": dict(by_kind.most_common()),
        "by_source": dict(by_source.most_common()),
        **{f"by_{k}": dict(c.most_common()) for k, c in utm.items() if c},
        "privacy": "bez osobních údajů: žádné e-maily, jména ani volný text",
    }


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError

    from . import mcp_server

    mcp_server.TOOL_PERMISSIONS.setdefault("kniha_reservations_summary", PERMISSION)

    @mcp.tool(description="Kniha (Rodinné příběhy): the web's reservations counted without personal data: "
                          "total, unique contacts, by ref code src (p01-p10, wo01, owner) with first/last day, by "
                          "day, by day and src, by kind (gift/self/lead_magnet), by section and UTM. Test "
                          "reservations are left out. since/until: YYYY-MM-DD (optional). Read-only; never "
                          "names or e-mails.")
    def kniha_reservations_summary(ctx: Context, since: str = "", until: str = "") -> dict:
        with session(ctx, "kniha_reservations_summary", since=since, until=until) as (conn, c):
            try:
                return summary(since.strip(), until.strip())
            except (ValueError, OSError) as e:
                raise ToolError(f"kniha_reservations_summary: {e}") from e
