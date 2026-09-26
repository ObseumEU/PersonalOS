"""Probes: HTTP health (with TLS expiry on the way), host resources, and the
apps' own run tables. Each returns plain numbers; deciding is detect.py's job."""

import http.client
import json
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def http_check(url: str, connect: str | None = None, timeout: float = 8.0, json_level: str | None = None) -> dict:
    """GET url; `connect` host:port routes it through the front proxy with the
    URL's host as SNI and Host header. ok = status < 500 for public URLs
    (a 401 or 403 still means the app answers), 2xx/3xx for internal ones."""
    u = urlparse(url)
    host = u.hostname or ""
    c_host, _, c_port = (connect or "").partition(":")
    port = int(c_port) if c_port else (u.port or (443 if u.scheme == "https" else 80))
    target = c_host or host
    t0 = time.monotonic()
    out = {"ok": False, "status": None, "ms": None, "tls_days": None, "detail": ""}
    try:
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            # SNI and the certificate check use the public name even when connecting to the proxy
            conn = http.client.HTTPConnection(target, port, timeout=timeout)
            sock = socket.create_connection((target, port), timeout=timeout)
            conn.sock = ctx.wrap_socket(sock, server_hostname=host)
            cert = conn.sock.getpeercert()
            if cert and cert.get("notAfter"):
                exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                out["tls_days"] = round((exp - datetime.now(timezone.utc)).total_seconds() / 86400, 1)
        else:
            conn = http.client.HTTPConnection(target, port, timeout=timeout)
        conn.request("GET", u.path or "/", headers={"Host": host, "User-Agent": "pos-sentinel"})
        r = conn.getresponse()
        body = r.read(4096)
        conn.close()
        out["status"] = r.status
        out["ms"] = int((time.monotonic() - t0) * 1000)
        public = bool(connect)
        out["ok"] = r.status < 500 if public else 200 <= r.status < 400
        if json_level and out["ok"]:
            try:
                out["level"] = json.loads(body).get(json_level)
            except (ValueError, AttributeError):
                pass
        if not out["ok"]:
            out["detail"] = f"HTTP {r.status}"
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        out["ms"] = int((time.monotonic() - t0) * 1000)
        out["detail"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def host(proc: str = "/proc", disks: list[str] | None = None) -> dict:
    """Memory, swap and load from /proc (the host's, a container sees them too)
    and disk use of the given mount points."""
    mem = {}
    try:
        with open(os.path.join(proc, "meminfo"), encoding="ascii") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.split()[0])
    except OSError:
        pass
    out: dict = {}
    if mem.get("MemTotal"):
        out["mem_avail_pct"] = round(100 * mem.get("MemAvailable", 0) / mem["MemTotal"], 1)
        out["mem_total_gb"] = round(mem["MemTotal"] / 1048576, 1)
    if mem.get("SwapTotal"):
        out["swap_pct"] = round(100 * (mem["SwapTotal"] - mem.get("SwapFree", 0)) / mem["SwapTotal"], 1)
    try:
        with open(os.path.join(proc, "loadavg"), encoding="ascii") as f:
            l1, l5, l15 = (float(x) for x in f.read().split()[:3])
        out.update(load1=l1, load5=l5, load15=l15, cpus=os.cpu_count() or 1)
    except (OSError, ValueError):
        pass
    for d in disks or []:
        try:
            st = os.statvfs(d)
            used = 1 - st.f_bavail / st.f_blocks if st.f_blocks else 0
            out.setdefault("disk_pct", {})[d] = round(100 * used, 1)
        except (OSError, AttributeError):
            pass
    return out


def _get_json(url: str, token: str | None = None, timeout: float = 10.0):
    req = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def pos_runs(pos_url: str, token: str) -> dict | None:
    """PersonalOS: finished runs and failed runs in the last hour (/api/sentinel/stats)."""
    try:
        d = _get_json(f"{pos_url}/api/sentinel/stats", token)
        return {"total": int(d["runs_hour"]), "failed": int(d["failed_hour"]), "detail": d.get("by_agent")}
    except (OSError, ValueError, KeyError):
        return None


def _epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def knowlage_runs(base: str, since: float) -> dict | None:
    """knowlage: sync runs (ok / error) that started in the last hour, over all sources."""
    try:
        sources = _get_json(f"{base}/api/sources")
        if isinstance(sources, dict):
            sources = sources.get("sources") or sources.get("items") or []
        total = failed = 0
        for s in sources[:50]:
            sid = s.get("id") if isinstance(s, dict) else s
            runs = _get_json(f"{base}/api/sources/{sid}/runs?limit=50")
            if isinstance(runs, dict):
                runs = runs.get("runs") or []
            for r in runs:
                if _epoch(r.get("startedAt") or r.get("started_at")) >= since and r.get("status") in ("ok", "error"):
                    total += 1
                    failed += r["status"] == "error"
        return {"total": total, "failed": failed}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def nexus_runs(dsn: str) -> dict | None:
    """Nexus: agent runs and workflow runs finished in the last hour (a read-only DB role)."""
    if not dsn:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=5) as c:
            c.execute("SET statement_timeout = 5000")
            r = c.execute("""SELECT count(*) FILTER (WHERE status = 'failed'),
                                    count(*) FILTER (WHERE status IN ('completed', 'failed', 'stopped'))
                             FROM runs WHERE created_at > now() - interval '1 hour'""").fetchone()
            w = c.execute("""SELECT count(*) FILTER (WHERE status = 'failed'),
                                    count(*) FILTER (WHERE status IN ('completed', 'failed', 'cancelled'))
                             FROM workflow_runs WHERE started_at > now() - interval '1 hour'""").fetchone()
            reason = c.execute("""SELECT left(e.payload->>'message', 200) FROM run_events e JOIN runs r ON r.id = e.run_id
                                  WHERE e.type = 'run_failed' AND r.created_at > now() - interval '1 hour'
                                  ORDER BY e.id DESC LIMIT 3""").fetchall()
        return {"total": r[1] + w[1], "failed": r[0] + w[0],
                "detail": {"agent_runs": [r[0], r[1]], "workflow_runs": [w[0], w[1]],
                           "last_reasons": [x[0] for x in reason if x[0]]}}
    except Exception:  # noqa: BLE001 - a missing table or grant is a quiet None, never a crash
        return None


def litellm_budgets(base: str, key: str) -> list[dict] | None:
    """Virtual keys (and teams) with a budget: spend and max_budget from LiteLLM's admin API."""
    if not key:
        return None
    out = []
    try:
        d = _get_json(f"{base}/key/list?return_full_object=true&size=100", key)
        for k in d.get("keys") or []:
            if isinstance(k, dict) and k.get("max_budget"):
                out.append({"name": k.get("key_alias") or k.get("key_name") or "key", "spend": k.get("spend") or 0,
                            "max_budget": k["max_budget"]})
        try:
            teams = _get_json(f"{base}/team/list", key)
            for t in teams if isinstance(teams, list) else []:
                if t.get("max_budget"):
                    out.append({"name": f"team:{t.get('team_alias') or t.get('team_id')}", "spend": t.get("spend") or 0,
                                "max_budget": t["max_budget"]})
        except (OSError, ValueError):
            pass
    except (OSError, ValueError, AttributeError):
        return None
    return out
