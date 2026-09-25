"""Quick-capture syntax (AGENTS-SPEC 6a).

    Call the bank tomorrow 15m #finance !high
    @ai summarise the lease
    Renew Acme due 1.10. #acme !p1 ~high +private

Tokens (anywhere in the text, English or Czech):
    #topic                     topic
    !high !p1 !must            priority 1   (!p2 !should: 2, !low !p3 !could: 3)
    15m 1h 1h30m               estimate
    today dnes tomorrow zítra  do date; also weekdays (mon, po, ...), 2026-10-01, 1.10.
    due / deadline / do <date> deadline instead of do date
    @ai @me @<name>            assignee (a known agent or person, otherwise someone outside)
    ~high ~low                 energy
    +private +public +team     visibility layer
"""

import re
from datetime import date, timedelta

from .core import today as _today

# Two-letter Czech forms like "po" or "ne" are left out: they are common words.
WEEKDAYS = {
    "mon": 0, "monday": 0, "pondeli": 0, "pondělí": 0,
    "tue": 1, "tuesday": 1, "út": 1, "utery": 1, "úterý": 1,
    "wed": 2, "wednesday": 2, "streda": 2, "středa": 2,
    "thu": 3, "thursday": 3, "čt": 3, "ctvrtek": 3, "čtvrtek": 3,
    "fri": 4, "friday": 4, "pá": 4, "patek": 4, "pátek": 4,
    "sat": 5, "saturday": 5, "sobota": 5,
    "sun": 6, "sunday": 6, "nedele": 6, "neděle": 6,
}
PRIORITY = {"high": 1, "p1": 1, "must": 1, "p2": 2, "should": 2, "med": 2, "low": 3, "p3": 3, "could": 3}
DEADLINE_WORDS = {"due", "deadline", "by", "termin", "termín"}


def parse_date(word: str, base: date | None = None) -> date | None:
    base = base or _today()
    w = word.lower().rstrip(",")
    if w in ("today", "dnes"):
        return base
    if w in ("tomorrow", "zitra", "zítra"):
        return base + timedelta(days=1)
    if w in WEEKDAYS:
        delta = (WEEKDAYS[w] - base.weekday()) % 7 or 7
        return base + timedelta(days=delta)
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", w):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})?", w):
        d = date(int(m[3]) if m[3] else base.year, int(m[2]), int(m[1]))
        if not m[3] and d < base:
            d = d.replace(year=base.year + 1)
        return d
    return None


def parse(text: str, base: date | None = None) -> dict:
    """Split quick-capture text into task fields. Unknown words stay in the title."""
    out: dict = {}
    title: list[str] = []
    words = text.split()
    i = 0
    while i < len(words):
        w = words[i]
        lw = w.lower()
        if lw in DEADLINE_WORDS and i + 1 < len(words) and (d := parse_date(words[i + 1], base)):
            out["deadline"] = d.isoformat()
            i += 2
            continue
        if w.startswith("#") and len(w) > 1:
            out["topic"] = w[1:].lower()
        elif w.startswith("!") and lw[1:] in PRIORITY:
            out["priority"] = PRIORITY[lw[1:]]
        elif w.startswith("~") and lw[1:] in ("high", "low"):
            out["energy"] = lw[1:]
        elif w.startswith("+") and lw[1:] in ("private", "public", "team"):
            out["visibility"] = lw[1:]
        elif w.startswith("@") and len(w) > 1:
            out["assignee"] = w[1:]
        elif m := re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?:in)?)?", lw):
            if m[1] or m[2]:
                out["estimate_min"] = int(m[1] or 0) * 60 + int(m[2] or 0)
            else:
                title.append(w)
        elif (d := parse_date(w, base)) and "do_date" not in out:
            out["do_date"] = d.isoformat()
        else:
            title.append(w)
        i += 1
    out["title"] = " ".join(title).strip() or text.strip()
    return out
