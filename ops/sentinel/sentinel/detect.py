"""Rules: measurements in, observations (possible incidents) out.

Every rule is deterministic code with a threshold from config.thresholds;
nothing here calls a model.
"""

import hashlib
import json
import time

from . import fingerprint as fpmod
from .incidents import Obs
from .redact import redact
from .store import Store


def _minute(t: float) -> int:
    return int(t // 60)


# ------------------------------------------------------------------ logs

def ingest_logs(s: Store, t: dict, service: str, container: str, lines: list[tuple[float, str]],
                now: float | None = None, learning: bool = False) -> list[Obs]:
    """Count error fingerprints and status codes of one container's new lines,
    then judge them: a new fingerprint at a meaningful rate, a known one
    spiking against its baseline, and the status-code rules."""
    now = now or time.time()
    batch: dict[str, dict] = {}
    for at, line in lines:
        at = at or now
        c = fpmod.classify(line)
        m = _minute(at)
        if c["status"] is not None:
            s.stat(container, "req", m, 1)
            if c["status"] >= 500:
                s.stat(container, "5xx", m, 1)
            elif c["status"] == 429:
                s.stat(container, "429", m, 1)
            elif c["status"] == 401:
                s.stat(container, "401", m, 1)
        if fpmod.QUOTA.search(line):
            s.stat(container, "quota", m, 1)
            _quota_sample(s, container, line, at)
        if not c["error"]:
            continue
        fp = fpmod.fp_of(service, c)
        b = batch.setdefault(fp, {"n": 0, "key": fpmod.key_of(c), "by_min": {}, "last": line, "at": at})
        b["n"] += 1
        b["by_min"][m] = b["by_min"].get(m, 0) + 1
        b["last"], b["at"] = line, at
    out: list[Obs] = []
    for fp, b in batch.items():
        new = False
        for m, n in b["by_min"].items():
            new = s.count_fp(fp, service, container, b["key"], m, n, b["at"]) or new
        sample = redact(b["last"])
        s.add_sample(fp, hashlib.sha1(fpmod.normalize(sample).encode()).hexdigest()[:12], sample, b["at"])
        if not learning:
            out += judge_fp(s, t, fp, service, container, b["key"], b["n"], now)
    if not learning:
        out += judge_status(s, t, service, container, now)
    return out


def _quota_sample(s: Store, container: str, line: str, at: float) -> None:
    fp = "quota:" + container
    s.add_sample(fp, hashlib.sha1(fpmod.normalize(line).encode()).hexdigest()[:12], redact(line), at, keep=10)


def judge_fp(s: Store, t: dict, fp: str, service: str, container: str, key: str, n_now: int, now: float) -> list[Obs]:
    row = s.one("SELECT first_seen FROM fp WHERE fp = ?", fp)
    w = int(t["window_min"])
    end = _minute(now) + 1
    in_window = s.fp_sum(fp, end - w, end)
    # an open incident for this fingerprint absorbs every new occurrence
    for kind in ("new_error", "error_spike"):
        if s.one("SELECT 1 FROM incidents WHERE status = 'open' AND service = ? AND kind = ? AND key = ?",
                 service, kind, fp):
            return [Obs(service, kind, fp, "medium", f"{service}: {key[:120]}", n_now, {"container": container},
                        container)]
    age_min = (now - row["first_seen"]) / 60 if row else 0
    if age_min <= 2 * w:
        if in_window >= t["new_fp_min"]:
            sev = "high" if in_window >= 10 * t["new_fp_min"] else "medium"
            return [Obs(service, "new_error", fp, sev, f"{service}: new error — {key[:120]}", in_window,
                        {"container": container, "per_window": in_window, "window_min": w}, container)]
        return []
    # baseline: the average per minute over the last 24 h before the window
    base_start = max(end - w - 1440, _minute(row["first_seen"]))
    base_minutes = max((end - w) - base_start, 1)
    baseline = s.fp_sum(fp, base_start, end - w) / base_minutes
    rate = in_window / w
    if rate >= t["spike_min_per_min"] and rate > t["spike_factor"] * baseline:
        sev = "high" if rate >= 10 * t["spike_min_per_min"] else "medium"
        return [Obs(service, "error_spike", fp, sev, f"{service}: error spike ×{rate / max(baseline, 0.01):.0f} — "
                    f"{key[:100]}", in_window,
                    {"container": container, "rate_per_min": round(rate, 1), "baseline_per_min": round(baseline, 3)},
                    container)]
    return []


def judge_status(s: Store, t: dict, service: str, container: str, now: float) -> list[Obs]:
    w = int(t["window_min"])
    end = _minute(now) + 1
    n = {m: s.stat_sum(container, m, end - w, end) for m in ("req", "5xx", "429", "401", "quota")}
    out = []
    if n["5xx"] >= t["http_5xx_min"] and n["5xx"] >= t["http_5xx_ratio"] * max(n["req"], 1):
        out.append(Obs(service, "http_5xx", container, "high", f"{container}: {n['5xx']} of {n['req']} requests "
                       f"answered 5xx in {w} min", n["5xx"], {"requests": n["req"], "5xx": n["5xx"]}, container))
    if n["429"] >= t["http_429_min"]:
        out.append(Obs(service, "rate_limited", container, "medium", f"{container}: {n['429']}× HTTP 429 in {w} min",
                       n["429"], {"requests": n["req"], "429": n["429"]}, container))
    if n["quota"] >= t["quota_min"]:
        out.append(Obs(service, "quota", container, "high", f"{container}: usage limit / quota errors "
                       f"({n['quota']} in {w} min)", n["quota"], {"quota_lines": n["quota"]}, container))
    if n["401"] >= t["auth_401_min"]:
        out.append(Obs(service, "auth_flood", container, "medium", f"{container}: {n['401']}× HTTP 401 in {w} min",
                       n["401"], {"401": n["401"], "requests": n["req"]}, container))
    return out


# ------------------------------------------------------------------ health and containers

def health(s: Store, t: dict, check: dict, result: dict, now: float) -> list[Obs]:
    name = check["name"]
    row = s.one("SELECT fails FROM checks WHERE name = ?", name)
    fails = 0 if result["ok"] else (row["fails"] if row else 0) + 1
    s.x("""INSERT INTO checks (name, service, ok, fails, last_ok, last_fail, detail) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (name) DO UPDATE SET ok = excluded.ok, fails = excluded.fails,
             last_ok = COALESCE(excluded.last_ok, checks.last_ok), last_fail = COALESCE(excluded.last_fail, checks.last_fail),
             detail = excluded.detail""",
        name, check["service"], int(result["ok"]), fails, now if result["ok"] else None,
        None if result["ok"] else now, json.dumps({k: result.get(k) for k in ("status", "ms", "detail", "tls_days", "level")}))
    out = []
    if fails >= t["health_fails"]:
        out.append(Obs(check["service"], "health", name, "high" if fails < 10 else "critical",
                       f"{name} is failing ({result.get('detail') or result.get('status')})", 1,
                       {"url": check["url"], "fails_in_a_row": fails, "last": result.get("detail") or result.get("status")},
                       check.get("restart")))
    if result.get("level") == "error":
        out.append(Obs(check["service"], "sync_error", name, "medium", f"{name}: reports sync level error", 1,
                       {"url": check["url"]}))
    days = result.get("tls_days")
    if days is not None and days < t["tls_days_warn"]:
        out.append(Obs(check["service"], "tls", name, "high" if days < t["tls_days_high"] else "medium",
                       f"{name}: TLS certificate expires in {days:.0f} days", 1, {"days_left": days}))
    return out


def containers(s: Store, t: dict, service_of, items: list[dict], now: float) -> list[Obs]:
    """items: inspected containers {name, status, health, restart_count, started_at, oom, exit_code, image, created}."""
    out = []
    for c in items:
        name, service = c["name"], service_of(c["name"])
        prev = s.one("SELECT * FROM containers WHERE name = ?", name)
        streaks = s.meta(f"streak:{name}") or {}
        down = streaks.get("down", 0) + 1 if c["status"] != "running" else 0
        unhealthy = streaks.get("unhealthy", 0) + 1 if c.get("health") == "unhealthy" else 0
        s.set_meta(f"streak:{name}", {"down": down, "unhealthy": unhealthy})
        if prev is not None and c["restart_count"] is not None and prev["restart_count"] is not None:
            delta = c["restart_count"] - prev["restart_count"]
            restarted = prev["started_at"] and c["started_at"] != prev["started_at"]
            if delta > 0:
                s.x("INSERT INTO restarts (container, at, n) VALUES (?, ?, ?)", name, now, delta)
            if restarted and c.get("oom"):
                out.append(Obs(service, "oom", name, "high", f"{name} was OOM-killed", 1,
                               {"restart_count": c["restart_count"]}, name))
        s.x("""INSERT INTO containers (name, restart_count, started_at, status, health, oom, image, created, seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET restart_count = excluded.restart_count,
               started_at = excluded.started_at, status = excluded.status, health = excluded.health, oom = excluded.oom,
               image = excluded.image, created = excluded.created, seen = excluded.seen""",
            name, c["restart_count"], c["started_at"], c["status"], c.get("health"), int(bool(c.get("oom"))),
            c.get("image"), c.get("created"), now)
        recent = s.one("SELECT COALESCE(SUM(n), 0) AS n FROM restarts WHERE container = ? AND at >= ?",
                       name, now - 60 * t["restart_window_min"])["n"]
        if recent >= t["restart_loop"]:
            out.append(Obs(service, "restart_loop", name, "high", f"{name} restarted {recent}× in "
                           f"{t['restart_window_min']} min", 1, {"restarts": recent}, name))
        if down >= t["down_streak"] and c.get("exit_code") != 0:
            out.append(Obs(service, "container_down", name, "high", f"{name} is {c['status']}"
                           + (f" (exit {c.get('exit_code')})" if c.get("exit_code") is not None else ""), 1,
                           {"status": c["status"], "exit_code": c.get("exit_code")}, name))
        if unhealthy >= t["unhealthy_streak"]:
            out.append(Obs(service, "unhealthy", name, "high", f"{name} reports unhealthy", 1, {}, name))
    return out


# ------------------------------------------------------------------ host, apps, budgets

def host(t: dict, h: dict) -> list[Obs]:
    out = []
    for path, pct in (h.get("disk_pct") or {}).items():
        if pct >= t["disk_pct"]:
            out.append(Obs("host", "disk", path, "critical" if pct >= t["disk_pct_critical"] else "high",
                           f"disk {path} is {pct:.0f}% full", 1, {"disk_pct": pct}))
    if h.get("mem_avail_pct") is not None and h["mem_avail_pct"] < t["mem_avail_pct"]:
        out.append(Obs("host", "memory", "mem", "high", f"only {h['mem_avail_pct']:.0f}% memory available", 1,
                       {"mem_avail_pct": h["mem_avail_pct"]}))
    if h.get("swap_pct") is not None and h["swap_pct"] >= t["swap_pct"]:
        out.append(Obs("host", "swap", "swap", "high" if h["swap_pct"] >= 98 else "medium",
                       f"swap is {h['swap_pct']:.0f}% used", 1, {"swap_pct": h["swap_pct"],
                                                                  "mem_avail_pct": h.get("mem_avail_pct")}))
    if h.get("load5") is not None and h["load5"] / max(h.get("cpus", 1), 1) >= t["load_per_cpu"]:
        out.append(Obs("host", "load", "load", "medium", f"load {h['load5']:.1f} on {h.get('cpus')} CPUs", 1,
                       {"load5": h["load5"], "cpus": h.get("cpus")}))
    return out


def runs(t: dict, app: str, stats: dict | None) -> list[Obs]:
    if not stats or stats["total"] < t["run_min"]:
        return []
    ratio = stats["failed"] / stats["total"]
    if ratio <= t["run_fail_ratio"]:
        return []
    return [Obs(app, "run_failures", "runs", "critical" if ratio >= 0.9 else "high",
                f"{app}: {stats['failed']} of {stats['total']} runs failed in the last hour", 1,
                {"failed": stats["failed"], "total": stats["total"], "ratio": round(ratio, 2),
                 **({"detail": stats["detail"]} if stats.get("detail") else {})})]


def budgets(t: dict, items: list[dict] | None) -> list[Obs]:
    out = []
    for b in items or []:
        ratio = (b["spend"] or 0) / b["max_budget"] if b["max_budget"] else 0
        if ratio >= t["budget_ratio"]:
            out.append(Obs("litellm", "budget", b["name"], "high" if ratio >= 1 else "medium",
                           f"LiteLLM budget {b['name']}: ${b['spend']:.2f} of ${b['max_budget']:.2f}", 1,
                           {"spend": b["spend"], "max_budget": b["max_budget"], "ratio": round(ratio, 2)}))
    return out
