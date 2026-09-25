"""The agenda: calendar events next to dated tasks.

Calendars come in as iCal (ICS) feeds, which works without any login: Google
Calendar ("Secret address in iCal format"), Microsoft 365 ("Publish calendar"),
Apple. Set POS_CALENDAR_ICS to one or more feeds separated by whitespace, each
optionally named: `Work|https://calendar.google.com/...basic.ics`. The URLs are
secrets and never leave the server; the API names calendars only. Feeds are
fetched at most every 10 minutes; recurring events are expanded.
"""

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from . import tasks
from .core import TZ, Ctx

CACHE_S = 600
_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()


def feeds() -> list[tuple[str, str]]:
    out = []
    for i, item in enumerate(os.environ.get("POS_CALENDAR_ICS", "").split()):
        name, _, url = item.rpartition("|")
        out.append((name or f"Calendar {i + 1}", url))
    return [(n, u.replace("webcal://", "https://", 1)) for n, u in out if u]


def _fetch(url: str) -> bytes:
    with _lock:
        hit = _cache.get(url)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    r = httpx.get(url, timeout=20, follow_redirects=True)
    r.raise_for_status()
    with _lock:
        _cache[url] = (time.monotonic(), r.content)
    return r.content


def _as_local(value) -> tuple[datetime, bool]:
    """(local datetime, all_day) from an iCal DATE or DATE-TIME."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ), False
    return datetime(value.year, value.month, value.day, tzinfo=TZ), True


def parse(ics: bytes, calendar: str, start: date, end: date) -> list[dict]:
    """Events overlapping [start, end) in local time, recurring ones expanded."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s, all_day = _as_local(ev.decoded("DTSTART"))
        e_raw = ev.decoded("DTEND") if ev.get("DTEND") else None
        e = _as_local(e_raw)[0] if e_raw is not None else s + (timedelta(days=1) if all_day else timedelta(hours=1))
        out.append({
            "id": f"{calendar}:{ev.get('UID', '')}:{s.isoformat()}",
            "title": str(ev.get("SUMMARY", "(no title)")),
            "start": s.isoformat(), "end": e.isoformat(), "all_day": all_day,
            "location": str(ev.get("LOCATION", "")) or None,
            "calendar": calendar,
        })
    return out


def agenda(conn, ctx: Ctx, start: date, days: int = 7) -> dict:
    end = start + timedelta(days=days)
    sources, events = [], []
    for name, url in feeds():
        try:
            events += parse(_fetch(url), name, start, end)
            sources.append({"name": name, "ok": True})
        except Exception as e:  # noqa: BLE001 - one broken feed must not hide the others
            sources.append({"name": name, "ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    events.sort(key=lambda e: (e["start"], e["title"]))
    dated = []
    rows = conn.execute(
        """SELECT id FROM tasks WHERE archived_at IS NULL AND status NOT IN ('done', 'someday')
           AND ((do_date >= ? AND do_date < ?) OR (deadline >= ? AND deadline < ?))
           ORDER BY COALESCE(do_date, deadline)""",
        (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
    ).fetchall()
    for r in rows:
        try:
            t = tasks.get(conn, ctx, r["id"])
        except Exception:  # noqa: BLE001 - not visible to this member
            continue
        for kind in ("do_date", "deadline"):
            if t.get(kind) and start.isoformat() <= t[kind] < end.isoformat():
                dated.append({"ref": t["ref"], "title": t["title"], "date": t[kind],
                              "kind": "deadline" if kind == "deadline" else "do", "status": t["status"],
                              "assignee_type": t.get("assignee_type"), "assignee_name": t.get("assignee_name")})
    return {"start": start.isoformat(), "days": days, "sources": sources, "events": events, "tasks": dated,
            "configured": bool(feeds()), "now": datetime.now(timezone.utc).astimezone(TZ).isoformat()}
