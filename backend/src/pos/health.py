"""The deep health check: GET /api/health/deep (the sentinel's fast probe and the blackbox exporter).

/api/health only says the process answers. On 2026-10-13 it kept answering 200 for 20 minutes while the owner's
app could not load (/api/auth/me 500 after 10 s, 31x "database is locked" in 15 min), so nothing alerted. This
one does what a request does: a real read, then takes the write lock (BEGIN IMMEDIATE) and gives it back
(ROLLBACK), with a short busy timeout. It reports "degraded" (HTTP 503) when that fails, plus the last minute of
the API: requests, 5xx and their share, p95 latency, "database is locked" errors and long transactions. Judging
those numbers (alerts, the restart) is the sentinel's job (ops/sentinel/sentinel/platform.py).

Unauthenticated (a probe has no session) and public through the web proxy, so it says nothing secret and runs
the probe at most once per CACHE_S, one at a time: a flood of requests cannot turn it into write-lock traffic.
Where the long transactions were opened (code locations) is shown only to internal callers (pos.metrics.allowed).
"""

import threading
import time
from pathlib import Path

from . import db, metrics

PROBE_BUSY_MS = 2000   # the probe's own busy timeout: the write lock must come within this
CACHE_S = 2.0
WARN_5XX_MIN, WARN_5XX_RATIO = 5, 0.05  # informational "warnings"; the sentinel has its own thresholds
WARN_LOCKED = 1

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "db": None}


def probe(db_path: Path, busy_ms: int = PROBE_BUSY_MS) -> dict:
    """A read and the write lock taken and released. {ok, ms, read_ms, write_ms, error?}."""
    t0 = time.perf_counter()
    out: dict = {"ok": False}
    conn = None
    try:
        conn = db.connect(db_path, busy_timeout_ms=busy_ms)
        conn.execute("SELECT COUNT(*) FROM actors").fetchone()
        t1 = time.perf_counter()
        out["read_ms"] = round((t1 - t0) * 1000, 1)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        out["write_ms"] = round((time.perf_counter() - t1) * 1000, 1)
        out["ok"] = True
    except Exception as e:  # noqa: BLE001 - every failure is a degraded answer, never a 500
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def _db_check(db_path: Path) -> dict:
    """The probe, cached for CACHE_S and single-flight (a caller waiting for a running probe gets its result)."""
    with _lock:
        if _cache["db"] is not None and time.monotonic() - _cache["at"] < CACHE_S:
            return {**_cache["db"], "cached": True}
        result = probe(db_path)
        _cache.update(at=time.monotonic(), db=result)
        return result


def deep(db_path: Path, internal: bool = False) -> tuple[dict, int]:
    """(body, HTTP status): 200 ok, 503 degraded (the database round-trip failed)."""
    check = _db_check(db_path)
    win = metrics.window()
    dbc = db.counts(metrics.WINDOW_S)
    held = db.held_transactions()
    warnings = []
    if win["errors_5xx"] >= WARN_5XX_MIN and win["rate_5xx"] >= WARN_5XX_RATIO:
        warnings.append(f"{win['errors_5xx']} of {win['requests']} requests answered 5xx in the last minute")
    if dbc["locked"] >= WARN_LOCKED:
        warnings.append(f"{dbc['locked']}x 'database is locked' in the last minute")
    if held:
        warnings.append(f"{len(held)} write transaction(s) open longer than {db.LONG_TX_S:g} s")
    status = "ok" if check["ok"] else "degraded"
    body = {
        "status": status,
        "db": {k: v for k, v in check.items() if k != "cached" or v},
        "last_minute": {**win, "db_locked": dbc["locked"], "long_tx": dbc["long_tx"]},
        "held_transactions": ([{"age_s": h["age_s"], "opened_at": h["opened_at"]} for h in held] if internal
                              else len(held)),
        "warnings": warnings,
    }
    if not check["ok"]:
        body["reason"] = f"database round-trip failed: {check.get('error', '')}"[:200]
    return body, 200 if check["ok"] else 503


def reset() -> None:
    """Tests: forget the cached probe."""
    with _lock:
        _cache.update(at=0.0, db=None)
