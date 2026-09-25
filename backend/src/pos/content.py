"""Helpers shared by files, notes and topics: topic slugs, tags, search terms."""

import json
import re
import sqlite3

from .db import HAS_FTS5, has_table
from .tasks import Invalid
from .visibility import LAYERS

MAX_TAGS = 20


def norm_topic(value: str | None) -> str | None:
    """A topic as tasks store it: lower case, no leading '#', spaces as dashes."""
    if value is None:
        return None
    v = re.sub(r"\s+", "-", str(value).strip().lstrip("#").strip().lower())
    if not v:
        return None
    if len(v) > 60 or not re.fullmatch(r"[\w.\-]+", v):
        raise Invalid("a topic is a short word: letters, digits, '-', '_' or '.'")
    return v


def norm_tags(value) -> list[str]:
    """Tags from a list or a comma separated string: lower case, unique, no '#'."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for item in items:
        t = re.sub(r"\s+", "-", str(item).strip().lstrip("#").strip().lower())[:40]
        if t and t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise Invalid(f"at most {MAX_TAGS} tags")
    return out


def check_visibility(value: str) -> None:
    if value not in LAYERS:
        raise Invalid(f"visibility must be one of {LAYERS}")


def tags_json(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False)


def words(q: str) -> list[str]:
    return re.findall(r"\w+", q or "")[:12]


def fts_query(q: str) -> str | None:
    """User text as a safe FTS5 query: every word must match (as a prefix)."""
    ws = words(q)
    return " ".join(f'"{w}"*' for w in ws) if ws else None


def use_fts(conn: sqlite3.Connection, table: str) -> bool:
    return HAS_FTS5 and has_table(conn, table)


def match_sql(conn: sqlite3.Connection, table: str, fts_table: str, like_cols: tuple[str, ...],
              q: str) -> tuple[str, list]:
    """SQL condition (and params) for rows of `table` matching the search text.
    FTS5 when the build has it, otherwise LIKE on every word."""
    if use_fts(conn, fts_table):
        return f"{table}.id IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?)", [fts_query(q)]
    conds, params = [], []
    for w in words(q):
        conds.append("(" + " OR ".join(f"{table}.{c} LIKE ?" for c in like_cols) + ")")
        params += [f"%{w}%"] * len(like_cols)
    return " AND ".join(conds), params
