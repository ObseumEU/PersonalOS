from datetime import date

from pos import actors, agenda, tasks
from pos.core import Ctx
from pos.db import connect, migrate

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup
DTSTART;TZID=Europe/Prague:20260921T090000
DTEND;TZID=Europe/Prague:20260921T091500
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:trip
DTSTART;VALUE=DATE:20260926
DTEND;VALUE=DATE:20260927
SUMMARY:Trip to Brno
LOCATION:Brno
END:VEVENT
BEGIN:VEVENT
UID:gone
DTSTART:20260924T100000Z
DTEND:20260924T110000Z
STATUS:CANCELLED
SUMMARY:Cancelled
END:VEVENT
END:VCALENDAR
"""


def test_parse_expands_recurring_events_and_skips_cancelled():
    evs = agenda.parse(ICS, "Work", date(2026, 9, 21), date(2026, 9, 28))
    titles = [e["title"] for e in evs]
    assert titles.count("Standup") == 3 and "Cancelled" not in titles
    trip = next(e for e in evs if e["title"] == "Trip to Brno")
    assert trip["all_day"] and trip["location"] == "Brno"
    standup = next(e for e in evs if e["title"] == "Standup")
    assert standup["start"] == "2026-09-21T09:00:00+02:00"


def test_agenda_merges_feeds_and_dated_tasks_without_leaking_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CALENDAR_ICS", "Work|https://example.invalid/secret.ics Broken|https://example.invalid/x.ics")
    monkeypatch.setattr(agenda, "_fetch", lambda url: ICS if "secret" in url else (_ for _ in ()).throw(OSError("down")))
    c = connect(tmp_path / "a.db")
    migrate(c)
    actors.ensure_builtin(c)
    me = Ctx(actors.owner_id(c))
    tasks.create(c, me, {"title": "Pay the invoice", "deadline": "2026-09-23", "status": "next"})
    out = agenda.agenda(c, me, date(2026, 9, 21), 7)
    assert [s["name"] for s in out["sources"]] == ["Work", "Broken"] and not out["sources"][1]["ok"]
    assert "secret" not in str(out)  # feed URLs are secrets
    assert any(t["title"] == "Pay the invoice" and t["kind"] == "deadline" for t in out["tasks"])
    assert len(out["events"]) == 4
