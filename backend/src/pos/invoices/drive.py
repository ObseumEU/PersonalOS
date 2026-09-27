"""Where an invoice goes on the owner's Drive, and the guard that it goes nowhere else.

Found on 2026-09-27 (read-only listing of the owner's folders):

    Obseum Ucetnictvi (business root, 1Al_ltq76WJfQhA7ygaWXWYHBXn5UfDgJ, owned by david.rosko@obseum.cz)
    ├── 2026
    │   └── Obseum s.r.o.
    │       └── Doklady
    │           ├── 01_Leden, 02_Unor, 03_Březen, … 08_Srpen, 09_Zari   ← NN_Month, diacritics mixed
    │           └── Chybějící faktury 2025
    ├── 2025 / Obseum s.r.o. / *Doklady nové / Doklady nové - září …  (older convention)
    └── Osobni (personal root, 1RPCP8Nw2v8c27kxh0JKVhjjYBz95uvg_: inside the business root; empty)

Business path: root → the year → `Obseum s.r.o.` → `Doklady` → the month. Names match case- and
diacritics-insensitively ("Obseum s.r.o" = "Obseum s.r.o."); a month folder matches by its number prefix
(`09_Zari`) or its Czech name (`Doklady nové - září`). A missing folder is created in the convention of its
siblings (`10_Říjen` next to `09_Zari`; `10_2026` next to `09_2026`).
Personal path: the Osobni folder; when it has year folders, the year (and the month when those exist).

`tree` is anything with children(folder_id) -> [{id, name, mimeType}], create_folder(parent_id, name) ->
{id, name} and parents(file_id) -> [ids]: the Drive client in production, a dict in tests.
"""

import re
from datetime import date

from .classify import BUSINESS, PERSONAL, fold

FOLDER = "application/vnd.google-apps.folder"
CZ_MONTHS = ["Leden", "Únor", "Březen", "Duben", "Květen", "Červen", "Červenec", "Srpen", "Září", "Říjen",
             "Listopad", "Prosinec"]
_CZ_FOLDED = [fold(m) for m in CZ_MONTHS]
COMPANY = "Obseum s.r.o."
DOKLADY = "Doklady"
MAX_DEPTH = 30


class Refused(Exception):
    """The code refuses the write: outside the two folder trees, or an unknown classification."""


def _folders(tree, folder_id: str) -> list[dict]:
    return sorted((c for c in tree.children(folder_id) if c.get("mimeType") == FOLDER),
                  key=lambda c: (c.get("createdTime") or "", c["name"]))


def year_of(name: str) -> int | None:
    m = re.fullmatch(r"\s*(\d{4})\s*", name or "")
    return int(m[1]) if m else None


def month_of(name: str) -> int | None:
    """09_Zari → 9, "Doklady nové - září" → 9, 2026-09 → 9, 09_2026 → 9; "Chybějící faktury 2025" → None."""
    n = (name or "").strip()
    if m := re.fullmatch(r"(\d{4})[-_. ](\d{1,2})", n):
        return int(m[2]) if 1 <= int(m[2]) <= 12 else None
    if m := re.match(r"(\d{1,2})(?!\d)(?:[_\-. ]|$)", n):
        return int(m[1]) if 1 <= int(m[1]) <= 12 else None
    words = re.findall(r"[a-z]+", fold(n))
    for i, month in enumerate(_CZ_FOLDED, start=1):
        if month in words:
            return i
    return None


def _is_company(name: str) -> bool:
    return re.fullmatch(r"obseum\s*s\.?\s*r\.?\s*o\.?", fold(name).strip()) is not None


def _is_doklady(name: str) -> bool:
    return fold(name).strip() == "doklady"


def month_folder_name(month: int, year: int, siblings: list[str]) -> str:
    """A new month folder in the convention of the existing ones (default `NN_Month`, with diacritics)."""
    named = [s for s in siblings if month_of(s)]
    if any(re.fullmatch(r"\d{1,2}_\d{4}", s.strip()) for s in named):
        return f"{month:02d}_{year}"
    if any(re.fullmatch(r"\d{4}-\d{1,2}", s.strip()) for s in named):
        return f"{year}-{month:02d}"
    if named and all(re.fullmatch(r"\d{1,2}", s.strip()) for s in named):
        return f"{month:02d}"
    if (m := next((re.match(r"(.*?)[\s-]*([^\s-]+)\s*$", s) for s in named
                   if not re.match(r"\d", s.strip())), None)) and fold(m[2]) in _CZ_FOLDED:
        return f"{m[1]} - {CZ_MONTHS[month - 1].lower()}" if m[1] else CZ_MONTHS[month - 1]
    sep = "_"
    for s in named:
        if sp := re.match(r"\d{1,2}([_\-. ]+)", s.strip()):
            sep = sp[1]
            break
    # Without diacritics only when the siblings drop them where Czech has them (02_Unor) and never keep them.
    accented = [s for s in named if CZ_MONTHS[month_of(s) - 1] != fold(CZ_MONTHS[month_of(s) - 1]).capitalize()]
    plain = bool(accented) and all(fold(s) == s.lower() for s in accented)
    label = fold(CZ_MONTHS[month - 1]).capitalize() if plain else CZ_MONTHS[month - 1]
    return f"{month:02d}{sep}{label}"


def _child(tree, parent: str, match, create_name: str | None, path: list[str], created: list[str]) -> str:
    for c in _folders(tree, parent):
        if match(c["name"]):
            path.append(c["name"])
            return c["id"]
    if create_name is None:
        raise Refused(f"složka {'/'.join(path) or 'kořen'} nemá podsložku, kterou hledám")
    made = tree.create_folder(parent, create_name)
    path.append(create_name)
    created.append("/".join(path))
    return made["id"]


def resolve(tree, classification: str, when: date, roots: dict) -> dict:
    """{folder_id, path, created}: the target folder, creating missing year/company/Doklady/month folders."""
    path: list[str] = []
    created: list[str] = []
    if classification == BUSINESS:
        root = roots[BUSINESS]
        year = _child(tree, root, lambda n: year_of(n) == when.year, str(when.year), path, created)
        company = _child(tree, year, _is_company, COMPANY, path, created)
        doklady = _child(tree, company, _is_doklady, DOKLADY, path, created)
        siblings = [c["name"] for c in _folders(tree, doklady)]
        month = _child(tree, doklady, lambda n: month_of(n) == when.month,
                       month_folder_name(when.month, when.year, siblings), path, created)
        return {"folder_id": month, "path": path, "created": created, "root": "Obseum Ucetnictvi"}
    if classification == PERSONAL:
        root = roots[PERSONAL]
        years = [c for c in _folders(tree, root) if year_of(c["name"])]
        if not years:  # no year folders: the files lie directly in Osobni
            return {"folder_id": root, "path": [], "created": [], "root": "Osobni"}
        year = _child(tree, root, lambda n: year_of(n) == when.year, str(when.year), path, created)
        months = [c["name"] for c in _folders(tree, year) if month_of(c["name"])]
        uses_months = months or any(month_of(c["name"]) for y in years for c in _folders(tree, y["id"]))
        if not uses_months:
            return {"folder_id": year, "path": path, "created": created, "root": "Osobni"}
        month = _child(tree, year, lambda n: month_of(n) == when.month,
                       month_folder_name(when.month, when.year, months), path, created)
        return {"folder_id": month, "path": path, "created": created, "root": "Osobni"}
    raise Refused(f"neznámá klasifikace {classification!r} (business | personal)")


def ancestors(tree, folder_id: str) -> list[str]:
    """The folder and every folder above it (by id, from Drive's parents)."""
    chain, current, seen = [folder_id], folder_id, {folder_id}
    for _ in range(MAX_DEPTH):
        parents = tree.parents(current) or []
        if not parents:
            break
        current = parents[0]
        if current in seen:
            break
        seen.add(current)
        chain.append(current)
    return chain


def guard(tree, folder_id: str, classification: str, roots: dict) -> None:
    """Refuse a target outside the tree of its classification. The personal root lies inside the business
    root, so a business target must also not be inside the personal tree."""
    chain = ancestors(tree, folder_id)
    if classification == BUSINESS:
        if roots[BUSINESS] not in chain:
            raise Refused(f"složka {folder_id} není ve stromu Obseum Ucetnictvi ({roots[BUSINESS]})")
        if roots[PERSONAL] in chain:
            raise Refused(f"firemní doklad nepatří do stromu Osobní ({folder_id})")
    elif classification == PERSONAL:
        if roots[PERSONAL] not in chain:
            raise Refused(f"složka {folder_id} není ve stromu Osobní ({roots[PERSONAL]})")
    else:
        raise Refused(f"neznámá klasifikace {classification!r}")
