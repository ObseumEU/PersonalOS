"""A tiny Docker Engine API client (stdlib only).

On the server it talks to a docker-socket-proxy that allows only reads
(containers, logs) and restarts, never exec, create, delete or volumes.
"""

import http.client
import json
import socket
import struct
from urllib.parse import quote, urlencode, urlparse


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, host: str = "unix:///var/run/docker.sock", timeout: float = 20.0):
        self.host, self.timeout = host, timeout

    def _conn(self) -> http.client.HTTPConnection:
        u = urlparse(self.host)
        if u.scheme == "unix":
            return _UnixConnection(u.path, self.timeout)
        return http.client.HTTPConnection(u.hostname, u.port or 2375, timeout=self.timeout)

    def _req(self, method: str, path: str, params: dict | None = None) -> bytes:
        c = self._conn()
        try:
            c.request(method, path + ("?" + urlencode(params) if params else ""))
            r = c.getresponse()
            body = r.read()
        finally:
            c.close()
        if r.status >= 300:
            raise DockerError(f"{method} {path}: {r.status} {body[:200]!r}")
        return body

    def containers(self) -> list[dict]:
        out = json.loads(self._req("GET", "/containers/json", {"all": "1"}))
        return [{"id": c["Id"], "name": c["Names"][0].lstrip("/"), "state": c.get("State"), "status": c.get("Status"),
                 "image": c.get("Image"), "labels": c.get("Labels") or {}} for c in out]

    def inspect(self, name: str) -> dict:
        return json.loads(self._req("GET", f"/containers/{quote(name)}/json"))

    def logs(self, name: str, since: float, until: float | None = None, tail: int | None = None,
             tty: bool = False) -> list[tuple[float, str]]:
        """(unix time, line) for stdout and stderr, oldest first."""
        params = {"stdout": "1", "stderr": "1", "timestamps": "1", "since": f"{since:.6f}"}
        if until:
            params["until"] = f"{until:.6f}"
        if tail:
            params["tail"] = str(tail)
        raw = self._req("GET", f"/containers/{quote(name)}/logs", params)
        return [parse_ts_line(line) for line in demux(raw, tty) if line]

    def restart(self, name: str, timeout_s: int = 10) -> None:
        self._req("POST", f"/containers/{quote(name)}/restart", {"t": str(timeout_s)})


def demux(raw: bytes, tty: bool) -> list[str]:
    """Docker's log stream: without a TTY each frame has an 8-byte header
    (stream, 0, 0, 0, size as big-endian uint32)."""
    if tty or not raw or raw[0] not in (0, 1, 2) or raw[1:4] != b"\x00\x00\x00":
        return raw.decode("utf-8", "replace").splitlines()
    i = 0
    buf = bytearray()
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        buf += raw[i + 8:i + 8 + size]
        i += 8 + size
    return bytes(buf).decode("utf-8", "replace").splitlines()


def parse_ts_line(line: str) -> tuple[float, str]:
    """'2026-09-26T08:27:09.752089123Z message' → (unix time, message)."""
    from datetime import datetime, timezone

    ts, _, msg = line.partition(" ")
    try:
        base, _, frac = ts.rstrip("Z").partition(".")
        t = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        t += float("0." + frac[:6]) if frac else 0.0
        return t, msg
    except ValueError:
        return 0.0, line
