"""The sentinel's small HTTP API (stdlib, one thread per request).

    GET  /healthz                    liveness (no token)
    GET  /fallback                   200 while PersonalOS answers, 503 with the
                                     open incidents while it does not (the
                                     Nexus service monitor checks this)
    GET  /api/status                 what the heartbeat carries (token)
    GET  /api/logs                   narrow, capped log reads for the Monitor
                                     agent: container, around, minutes<=30,
                                     fingerprint, grep, limit<=100 (token)
    POST /api/incidents/<ref>/ack    the Monitor agent's classification;
                                     resolve=true closes it (token)
    POST /api/test/inject            synthetic log lines (token, and only
                                     with SENTINEL_TEST_HOOK=1)

Everything returned from logs is redacted and truncated line by line.
"""

import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import detect
from . import fingerprint as fpmod
from .core import Sentinel
from .redact import redact

log = logging.getLogger("sentinel.api")
MAX_MINUTES, MAX_LIMIT, MAX_SCAN = 30, 100, 20_000
CLASSES = ("transient", "config", "capacity", "code_bug", "external_quota")


def _when(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def read_logs(sen: Sentinel, container: str, around: float, minutes: float, fingerprint: str | None = None,
              grep: str | None = None, limit: int = 60, errors_only: bool = True) -> dict:
    """Lines of one watched container around a time, filtered, deduplicated
    (repeats become '(×n)'), redacted and capped."""
    minutes = max(1.0, min(float(minutes), MAX_MINUTES))
    limit = max(1, min(int(limit), MAX_LIMIT))
    since, until = around - 30 * minutes, min(around + 30 * minutes, time.time())
    service = sen.service_of(container)
    grep_rx = re.compile(re.escape(grep[:80]), re.I) if grep else None
    with sen.lock:
        known = container in getattr(sen, "_running", set()) or sen.s.one(
            "SELECT 1 FROM containers WHERE name = ?", container) is not None
    if not sen.watched(container, "logs") or not known:
        # a synthetic or vanished source: what the sentinel kept (samples) is all there is
        with sen.lock:
            lines = sen.s.samples(fingerprint, limit) if fingerprint else []
        return {"container": container, "lines": lines, "scanned": 0, "matched": len(lines), "source": "samples"}
    raw = sen.docker.logs(container, since=since, until=until, tty=getattr(sen, "_tty", {}).get(container, False))
    raw = raw[-MAX_SCAN:]
    out, last_key, repeat, matched = [], None, 0, 0
    for _, line in raw:
        c = fpmod.classify(line)
        if fingerprint and fpmod.fp_of(service, c) != fingerprint:
            continue
        if grep_rx and not grep_rx.search(line):
            continue
        if errors_only and not fingerprint and not grep_rx and not (c["error"] or (c["status"] or 0) >= 400
                                                                  or fpmod.QUOTA.search(line)):
            continue
        matched += 1
        key = fpmod.key_of(c)
        if key == last_key:
            repeat += 1
            continue
        if repeat:
            out[-1] += f"  (×{repeat + 1})"
        out.append(redact(line, 300))
        last_key, repeat = key, 0
    if repeat and out:
        out[-1] += f"  (×{repeat + 1})"
    truncated = len(out) > limit
    return {"container": container, "since": since, "until": until, "lines": out[-limit:], "scanned": len(raw),
            "matched": matched, "truncated": truncated, "source": "docker"}


def make_handler(sen: Sentinel):
    token = sen.cfg["token"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "pos-sentinel"

        def log_message(self, fmt, *args):  # quiet: only errors go to the log
            pass

        def _send(self, code: int, body, ctype: str = "application/json") -> None:
            data = (json.dumps(body, default=str) if ctype == "application/json" else str(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "")
            ok = bool(token) and given.startswith("Bearer ") and hmac.compare_digest(given[7:].strip().encode(),
                                                                                    token.encode())
            if not ok:
                self._send(401, {"detail": "unauthorized"})
            return ok

        def _body(self) -> dict:
            n = min(int(self.headers.get("Content-Length") or 0), 200_000)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/healthz":
                age = time.time() - (sen.last_tick or sen.started_at)
                return self._send(200 if age < 5 * sen.cfg["interval_s"] else 503,
                                  {"ok": age < 5 * sen.cfg["interval_s"], "last_tick_age_s": int(age)})
            if u.path == "/fallback":
                if sen.pos_down_since:
                    return self._send(503, sen.fallback_text(time.time()), "text/plain")
                return self._send(200, "PersonalOS reachable\n", "text/plain")
            if not self._authorized():
                return None
            if u.path == "/api/status":
                return self._send(200, sen.status())
            if u.path == "/api/logs":
                container = q.get("container", "")
                if not container:
                    return self._send(422, {"detail": "container is required"})
                try:
                    out = read_logs(sen, container, _when(q.get("around"), time.time()), float(q.get("minutes") or 10),
                                    q.get("fingerprint") or None, q.get("grep") or None, int(q.get("limit") or 60),
                                    q.get("all") != "1")
                except Exception as e:  # noqa: BLE001
                    return self._send(502, {"detail": f"{type(e).__name__}: {str(e)[:160]}"})
                return self._send(200, out)
            return self._send(404, {"detail": "not found"})

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            if not self._authorized():
                return None
            body = self._body()
            m = re.fullmatch(r"/api/incidents/([0-9a-f]+)-(\d+)/ack", u.path)
            if m:
                if m.group(1) != sen.instance:
                    return self._send(404, {"detail": "another sentinel instance"})
                cls = body.get("classification")
                if cls not in CLASSES:
                    return self._send(422, {"detail": f"classification must be one of {CLASSES}"})
                iid = int(m.group(2))
                with sen.lock:
                    if sen.inc.get(iid) is None:
                        return self._send(404, {"detail": "no such incident"})
                    sen.inc.note(iid, f"Monitor: {cls} — {str(body.get('summary') or '')[:200]}")
                    sen.s.x("UPDATE incidents SET classification = ? WHERE id = ?", cls, iid)
                    if body.get("resolve"):
                        sen.inc.resolve(iid, "closed by the Monitor agent", classification=cls)
                    row = dict(sen.inc.get(iid))
                return self._send(200, {"id": m.group(0), "status": row["status"], "classification": cls})
            if u.path == "/api/test/inject":
                if not sen.cfg.get("test_hook"):
                    return self._send(404, {"detail": "the test hook is off (SENTINEL_TEST_HOOK=1)"})
                return self._send(200, inject(sen, body))
            return self._send(404, {"detail": "not found"})

    return Handler


def inject(sen: Sentinel, body: dict) -> dict:
    """Feed synthetic lines through the real pipeline (fingerprint → incident →
    event), for the end-to-end check. Service and container are prefixed
    `sentinel-test` so nothing real is ever touched (the runbook allowlist has
    no such container)."""
    service = "sentinel-test"
    container = "sentinel-test-" + re.sub(r"[^a-z0-9-]", "", str(body.get("container") or "app"))[:20]
    line = str(body.get("line") or "ERROR SyntheticTestError: sentinel end-to-end check")[:300]
    count = max(1, min(int(body.get("count") or 20), 5000))
    now = time.time()
    with sen.lock:
        obs = detect.ingest_logs(sen.s, sen.t, service, container, [(now, line)] * count, now, learning=False)
        out = sen.process(obs, now)
        fp = fpmod.fp_of(service, fpmod.classify(line))
        row = sen.s.one("SELECT id FROM incidents WHERE key = ? AND status = 'open'", fp)
    return {**out, "fingerprint": fp, "incident": sen.incident_ref(row["id"]) if row else None}


def serve(sen: Sentinel, listen: str) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(sen))
    threading.Thread(target=httpd.serve_forever, daemon=True, name="sentinel-api").start()
    log.info("API on %s", listen)
    return httpd
