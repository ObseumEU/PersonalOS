"""Prometheus metrics for the API (GET /metrics, internal only).

Light and dependency-free: a pure ASGI middleware counts every request and
times it by route template (`/api/tasks/{task_id}`, never the raw path, so the
series stay few), and a scrape-time collector reads the business numbers from
the database (run outcomes, the review queue, cost per day).

Series:
- pos_http_requests_total{method, route, status}       counter (status: 2xx, 3xx, 4xx, 5xx)
- pos_http_request_duration_seconds{method, route}     histogram (long-lived streams excluded)
- pos_sqlite_locked_total                              counter ("database is locked" in a request)
- pos_runs_total{status, kind}                         counter (every run ever, by outcome)
- pos_runs_running                                     gauge
- pos_review_queue                                     gauge (tasks in review)
- pos_needs_me{kind}                                   gauge (what waits for the owner)
- pos_cost_usd_today / pos_cost_usd_total              gauge / counter (engine_usage, UTC day)
- pos_live_subscribers                                 gauge (open /api/stream connections)

Access: the API is not published anywhere (the web nginx proxies /api/ only and
the front proxy reaches the web nginx), so /metrics is reachable only inside
the Docker networks. On top of that it answers only (a) a bearer token equal to
POS_METRICS_TOKEN when that is set, or (b) a direct request from a private
address that did not come through a proxy (no X-Forwarded-For). The scraper is
svr03's Alloy over the shared proxy network (deploy/observability/svr03).
"""

import ipaddress
import os
import sqlite3
import threading
import time
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

from starlette.responses import PlainTextResponse, Response

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
# Long-lived by design: counted, never timed (they would own the p95).
UNTIMED = ("/api/stream", "/api/chat/stream", "/api/worker/next", "/mcp")

_lock = threading.Lock()
_requests: dict[tuple[str, str, str], int] = {}
_hist: dict[tuple[str, str], list] = {}  # (method, route) -> [bucket counts..., sum, count]
_locked = 0


def _route_of(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None) or getattr(route, "path_format", None)
    if path:
        return path
    raw = scope.get("path", "")
    if raw.startswith("/mcp"):
        return "/mcp"
    if raw.startswith("/assets/") or not raw.startswith("/api"):
        return "other"
    return "unmatched"  # a 404 under /api: one series, not one per probed path


def observe(method: str, route: str, status: int, seconds: float | None) -> None:
    key = (method, route, f"{status // 100}xx")
    with _lock:
        _requests[key] = _requests.get(key, 0) + 1
        if seconds is None:
            return
        h = _hist.get((method, route))
        if h is None:
            h = _hist[(method, route)] = [0] * (len(BUCKETS) + 2)
        i = bisect_left(BUCKETS, seconds)
        if i < len(BUCKETS):  # beyond the last bucket: only +Inf (the count)
            h[i] += 1
        h[-2] += seconds
        h[-1] += 1


def count_locked() -> None:
    global _locked
    with _lock:
        _locked += 1


class Metrics:
    """Pure ASGI (streaming responses pass through untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = [500]

        async def _send(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as e:
            if "database is locked" in str(e):
                count_locked()
            raise
        finally:
            route = _route_of(scope)
            if route != "other" and route != "/metrics":
                timed = not any(route.startswith(p) for p in UNTIMED)
                observe(scope.get("method", "GET"), route, status[0],
                        time.perf_counter() - start if timed else None)


# ------------------------------------------------------------------ exposition

def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kv.items()) + "}" if kv else ""


def _business(db_path: Path) -> list[str]:
    from .db import connect

    out: list[str] = []
    conn = connect(db_path)
    try:
        out += ["# HELP pos_runs_total Agent runs by outcome (every run so far).", "# TYPE pos_runs_total counter"]
        running = 0
        for r in conn.execute("SELECT status, kind, COUNT(*) AS n FROM runs GROUP BY status, kind"):
            if r["status"] == "running":
                running += r["n"]
                continue
            out.append(f"pos_runs_total{_labels(status=r['status'], kind=r['kind'])} {r['n']}")
        out += ["# TYPE pos_runs_running gauge", f"pos_runs_running {running}"]
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'review'").fetchone()[0]
        out += ["# HELP pos_review_queue Tasks waiting in review.", "# TYPE pos_review_queue gauge",
                f"pos_review_queue {n}"]
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today, total = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN at >= ? THEN cost_usd END), 0), COALESCE(SUM(cost_usd), 0) "
            "FROM engine_usage", (day,)).fetchone()
        out += ["# HELP pos_cost_usd_today Engine cost since 00:00 UTC.", "# TYPE pos_cost_usd_today gauge",
                f"pos_cost_usd_today {today:.6f}",
                "# TYPE pos_cost_usd_total counter", f"pos_cost_usd_total {total:.6f}"]
        try:
            from . import actors, needs_me
            from .core import Ctx

            counts = needs_me.collect(conn, Ctx(actors.owner_id(conn), via="api"))["counts"]
            out.append("# TYPE pos_needs_me gauge")
            out += [f"pos_needs_me{_labels(kind=k)} {v}" for k, v in sorted(counts.items())]
        except Exception:  # noqa: BLE001 - a scrape never fails on one number
            pass
    except sqlite3.Error as e:
        if "locked" in str(e):
            count_locked()
        out.append("# business metrics unavailable")
    finally:
        conn.close()
    return out


def render(db_path: Path | None = None) -> str:
    lines = ["# HELP pos_http_requests_total API requests by route template and status class.",
             "# TYPE pos_http_requests_total counter"]
    with _lock:
        reqs = dict(_requests)
        hist = {k: list(v) for k, v in _hist.items()}
        locked = _locked
    for (m, r, s), n in sorted(reqs.items()):
        lines.append(f"pos_http_requests_total{_labels(method=m, route=r, status=s)} {n}")
    lines += ["# HELP pos_http_request_duration_seconds API latency by route template.",
              "# TYPE pos_http_request_duration_seconds histogram"]
    for (m, r), h in sorted(hist.items()):
        acc = 0
        for i, le in enumerate(BUCKETS):
            acc += h[i]
            lines.append(f"pos_http_request_duration_seconds_bucket{_labels(method=m, route=r, le=le)} {acc}")
        lines.append(f"pos_http_request_duration_seconds_bucket{_labels(method=m, route=r, le='+Inf')} {h[-1]}")
        lines.append(f"pos_http_request_duration_seconds_sum{_labels(method=m, route=r)} {h[-2]:.6f}")
        lines.append(f"pos_http_request_duration_seconds_count{_labels(method=m, route=r)} {h[-1]}")
    lines += ["# HELP pos_sqlite_locked_total Requests that failed on 'database is locked'.",
              "# TYPE pos_sqlite_locked_total counter", f"pos_sqlite_locked_total {locked}"]
    from . import live

    lines += ["# TYPE pos_live_subscribers gauge", f"pos_live_subscribers {live.subscriber_count()}"]
    if db_path is not None:
        lines += _business(db_path)
    return "\n".join(lines) + "\n"


def _private(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return host == "testclient"
    return ip.is_private or ip.is_loopback


def allowed(headers, client_host: str | None) -> bool:
    token = os.environ.get("POS_METRICS_TOKEN", "")
    auth = headers.get("authorization", "")
    if token and auth == f"Bearer {token}":
        return True
    if token and auth:
        return False
    return "x-forwarded-for" not in headers and "x-real-ip" not in headers and _private(client_host)


def endpoint(db_path: Path):
    from starlette.requests import Request

    async def metrics(request: Request) -> Response:
        if not allowed(request.headers, request.client.host if request.client else None):
            return PlainTextResponse("not found", status_code=404)
        import asyncio

        body = await asyncio.to_thread(render, db_path)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    return metrics
