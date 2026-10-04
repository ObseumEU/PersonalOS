"""One-off: the owner's pending decisions as cards, and the first company goals (fix package 2).

    python -m pos.owner_seed decisions            # dry run: the cards that would be made
    python -m pos.owner_seed decisions --apply    # make them (deduped: an open card on the topic stays)
    python -m pos.owner_seed goals                # dry run: the goals, with today's numbers
    python -m pos.owner_seed goals --apply        # propose them (status proposed; the CEO confirms)

Inside Docker: `docker compose exec api python -m pos.owner_seed decisions --apply`.

The decisions come from the prod record (2026-09-25..10-04): they never reached the owner as one
clear ask. Each is a decision card from the CEO (pos.asks: options, a recommendation, the default
after 72 h); a card whose answer the platform cannot carry out by itself (a credential only he can
create, a price that binds customers) has no default and waits for his click. Running it again
makes nothing new while the card is open (the same asker and topic: pos.asks dedup).

The goals are proposals with metric, baseline, current and target, owned by the leads; the CEO
confirms them (goal_upsert status=active) and updates them from then on.
"""

import argparse
import statistics
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, asks, goals
from .core import Ctx

DECISIONS = [
    {
        # The recommendation is the Kniha Lead's, from the record (not a guess): plan/10-decision-log.md
        # 2026-10-01 "Pracovní cena pilotu" (T-244) and the package plan/13-balik-znacka-cena-garance.md
        # (T-282), with the Hormozi reasoning cited there.
        "topic": "owner-decision kniha-cena-pilotu",
        "title": "Cena Knihy pro placený pilot",
        "why": "Bez ceny nelze pilot prodávat; balík značka + cena + garance čeká od 29. 9. (T-282).",
        "details": ("Návrh Kniha Leada (decision log Knihy 1. 10., T-244; balík plan/13, T-282): placený pilot "
                    "pro 10 rodin, rodiny 1–5 za 1 990 Kč (−50 % z plné ceny 3 990 Kč), rodiny 6–10 za 2 490 Kč, "
                    "poté zakladatelská cena 3 490 Kč; jen jádro, bez bonusů; povinná zpětná vazba; podmíněná "
                    "garance (vrácení peněz, když vypravěč 60 dní neodpoví nebo rodina neschválí náhled ani po "
                    "2 kolech). Důvod (Hormozi): sleva s pravdivým důvodem a zdražování po krocích, placený "
                    "pilot je skutečný test poptávky; 1 990 Kč ≈ 1 645 Kč bez DPH je nad variabilními náklady "
                    "≈ 1 195 Kč. Původní plán (plan/02) měl rodiny 1–5 zdarma. Do rozhodnutí oslovení běží jen "
                    "s nezávazným „kolem 2 000 Kč“ (T-246, T-507)."),
        "options": ["Návrh Kniha Leada: rodiny 1–5 za 1 990 Kč, 6–10 za 2 490 Kč, pak 3 490 Kč",
                    "Původní plán: rodiny 1–5 zdarma, pak 3 490 Kč"],
        "recommendation": "Návrh Kniha Leada: rodiny 1–5 za 1 990 Kč, 6–10 za 2 490 Kč, pak 3 490 Kč",
        "default_after_hours": 0,  # a price binds customers: only his click
        "source": 282,
    },
    {
        "topic": "owner-decision kniha-davka-kontaktu",
        "title": "Tvoje dávka 30–50 osobních zpráv pro pilot Knihy",
        "why": "Warm outreach stojí na nule (0 oslovených, 0 leadů, #kniha 2. 10.); skripty jsou připravené (T-246).",
        "details": ("Kniha Growth & Sales má balík pro tebe (marketing/warm-outreach-owner-balik.md, T-389) a "
                    "skripty pro 30–50 zpráv z tvých kanálů (T-246). Posíláš je ty: jde o tvé osobní kontakty."),
        "options": ["Pošlu celou dávku 30–50 zpráv tento týden", "Pošlu jen 10 nejbližším kontaktům",
                    "Nepošlu, pilot jen přes partnery"],
        "recommendation": "Pošlu jen 10 nejbližším kontaktům",
        "default_after_hours": 72,
        "source": 284,
    },
    {
        "topic": "owner-decision obseum-ai-provize",
        "title": "Odměna obchodníků Obseum AI",
        "why": "Inzerát i oslovení obchodníků běží bez konkrétní provize, dokud ji neschválíš (CEO, 1. 10.).",
        "details": ("Plán obchodu v2 je v poznámce 20 (Head of Growth, T-408). Model A: jen provize, 15 % z fáze 1 "
                    "+ 10 % z opakované tržby 12 měsíců, bonus 2 000 Kč za kvalifikovanou schůzku; 2–3 partneři "
                    "na 3 měsíce."),
        "options": ["Model A: jen provize (15 % fáze 1 + 10 % opakované 12 měs., 2 000 Kč za schůzku)",
                    "Fix + nižší provize", "Zatím bez obchodníků"],
        "recommendation": "Model A: jen provize (15 % fáze 1 + 10 % opakované 12 měs., 2 000 Kč za schůzku)",
        "default_after_hours": 72,
        "source": 408,
    },
    {
        "topic": "owner-decision sre-pristup-svr03",
        "title": "Přístup SRE na svr03 (úzký SSH credential)",
        "why": "Zálohy po výpadku 29. 9. se nedohnaly a nikdo z týmu nemá shell na svr03 (T-376).",
        "details": ("SRE žádá credential `svr03-sre-ssh` jen pro čtení (systemctl list-timers, journalctl, "
                    "ls /backups) a spuštění zálohovacích jednotek. Založit ho můžeš jen ty."),
        "options": ["Založím úzký credential svr03-sre-ssh", "Ne, zálohy hlídá jen kód a deploy"],
        "recommendation": "Založím úzký credential svr03-sre-ssh",
        "default_after_hours": 0,  # only he can create a credential
        "source": 376,
        "still_open": "SELECT 1 FROM tasks WHERE id = 376 AND status != 'done' AND archived_at IS NULL",
    },
]


def _ceo(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM actors WHERE role = 'ceo' AND archived_at IS NULL ORDER BY id").fetchone()
    return row["id"] if row else None


def decisions(conn: sqlite3.Connection, apply: bool = False) -> list[str]:
    ceo = _ceo(conn)
    if ceo is None:
        return ["no CEO: nothing to do"]
    out = []
    for d in DECISIONS:
        if d.get("still_open") and not conn.execute(d["still_open"]).fetchone():
            out.append(f"skip (no longer relevant): {d['title']}")
            continue
        src = d.get("source")
        if src and not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (src,)).fetchone():
            src = None
        if not apply:
            out.append(f"card: {d['title']} · {len(d['options'])} options · default "
                       f"{str(d['default_after_hours']) + ' h' if d['default_after_hours'] else 'none'}")
            continue
        r = asks.ask(conn, Ctx(ceo, via="system"), title=d["title"], why=d["why"], details=d["details"],
                     options=d["options"], recommendation=d["recommendation"], kind="decision", blocking=False,
                     task_id=src, topic=d["topic"], default_after_hours=d["default_after_hours"])
        out.append(f"{'exists' if r['deduped'] else 'made'}: {r['ref']} {d['title']}")
    if apply:
        conn.commit()
    return out


# ------------------------------------------------------------------ goals

def _week() -> tuple[str, str]:
    u = datetime.now(timezone.utc)
    return (u - timedelta(days=7)).isoformat(timespec="seconds"), u.isoformat(timespec="seconds")


def _support_hours(conn: sqlite3.Connection) -> float | None:
    """Median hours from a customer's mail task to its done, over the last 30 days."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    hours = []
    for r in conn.execute("""SELECT created_at, completed_at FROM tasks WHERE (source = 'event:gmail' OR source LIKE
                             'support:%') AND completed_at IS NOT NULL AND created_at >= ?""", (since,)):
        try:
            a, b = datetime.fromisoformat(r["created_at"]), datetime.fromisoformat(r["completed_at"])
        except (TypeError, ValueError):
            continue
        hours.append((b - a).total_seconds() / 3600)
    return round(statistics.median(hours), 1) if hours else None


def _business_share(conn: sqlite3.Connection) -> float | None:
    from . import business

    s, u = _week()
    share = business.cost_split(conn, s, u).get("business_share")
    return round(share * 100, 1) if share is not None else None


def goal_specs(conn: sqlite3.Connection) -> list[dict]:
    due = "2026-10-31"
    return [
        {"title": "Kniha pilot: oslovené kontakty", "metric": "oslovené kontakty (partneři + tvoje dávka)",
         "baseline": 0, "current": 0, "target_value": 50, "owner": "Kniha Growth & Sales", "due": due,
         "target": "50 oslovených kontaktů do 31. 10.",
         "why": "Pilot stojí na nule: 0 oslovených k 2. 10. (#kniha); bez oslovení nejsou rozhovory ani platby."},
        {"title": "Kniha pilot: rozhovory se zájemci", "metric": "uskutečněné rozhovory", "baseline": 0,
         "current": 0, "target_value": 10, "owner": "Kniha Lead", "due": due,
         "target": "10 rozhovorů do 31. 10.", "why": "Rozhovor ověří nabídku a cenu před platbou."},
        {"title": "Kniha pilot: zaplacené objednávky", "metric": "zaplacené pilotní objednávky", "baseline": 0,
         "current": 0, "target_value": 5, "owner": "Kniha Lead", "due": "2026-11-15",
         "target": "5 zaplacených objednávek do 15. 11.", "why": "Placený pilot je důkaz, že Kniha je byznys."},
        {"title": "Obseum AI: oslovení prospekti a obchody", "metric": "oslovení prospekti", "baseline": 0,
         "current": 0, "target_value": 60, "owner": "Head of Growth", "due": due,
         "target": "60 oslovených (10–20 týdně) a 2 podepsané obchody do 31. 10.",
         "why": "Plán obchodu v2 (poznámka 20) je hotový; oslovení obchodníků CEO schválil 1. 10."},
        {"title": "Zákaznická podpora: rychlost odpovědi", "metric": "medián hodin do první odpovědi zákazníkovi",
         "baseline": None, "current": None, "target_value": 24,
         "owner": "Head of Customer Success", "due": due, "target": "medián pod 24 h",
         "why": ("Zákazník bez odpovědi je ztracený zákazník. Dnes se to neměří (úkoly z pošty se zavírají za "
                 f"{_support_hours(conn)} h bez odpovědi): první číslo doplní Head of Customer Success.")},
        {"title": "Platforma: podíl byznysu na nákladech", "metric": "% nákladů na byznysové úkoly (7 dní)",
         "baseline": _business_share(conn), "current": _business_share(conn), "target_value": 50,
         "owner": "CEO", "due": due, "target": "≥ 50 % nákladů na byznys",
         "why": "Agenti mají vydělávat, ne hlavně opravovat platformu (cíl CEO business focus)."},
    ]


def seed_goals(conn: sqlite3.Connection, apply: bool = False) -> list[str]:
    goals.ensure_schema(conn)
    out = []
    by = Ctx(actors.system_id(conn), via="system")  # proposals: the CEO confirms them
    for g in goal_specs(conn):
        if conn.execute("SELECT 1 FROM goals WHERE title = ? AND archived_at IS NULL", (g["title"],)).fetchone():
            out.append(f"exists: {g['title']}")
            continue
        if not actors.find_by_name(conn, g["owner"]):
            g["owner"] = None
        line = (f"{g['title']}: {g['metric']} {g['baseline']} → now {g['current']} → target {g['target_value']} "
                f"by {g['due']} ({g['owner'] or 'no owner'})")
        if apply:
            made = goals.create(conn, by, {k: v for k, v in g.items() if v is not None})
            line = f"proposed #{made['id']} " + line
        out.append(line)
    if apply:
        conn.commit()
    return out


def main() -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(prog="python -m pos.owner_seed")
    p.add_argument("what", choices=("decisions", "goals"))
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    conn = connect(get_settings().db_path)
    try:
        lines = decisions(conn, a.apply) if a.what == "decisions" else seed_goals(conn, a.apply)
    finally:
        conn.close()
    print("\n".join(lines))


if __name__ == "__main__":
    main()
