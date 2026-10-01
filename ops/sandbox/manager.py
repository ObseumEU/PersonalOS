"""The sandbox manager (docs/SANDBOX.md): every agent's own computer, one Docker container each.

Two servers in one process:

- the control API (port 8200, bearer SANDBOX_TOKEN, only PersonalOS's api calls it): start an
  agent's container on first use, run commands in it, read and write its files, reset it;
- the egress proxy (port 3128, on the sandbox network only): the containers sit on an internal
  Docker network with no route out, and HTTP(S)_PROXY points here. The proxy connects only to
  public addresses (the LAN, the host, other containers and cloud metadata are refused), plus
  the PersonalOS api (pos-api:8000) by name.

Containers: `pos-sbx-<agent id>` from SANDBOX_IMAGE, root inside but with no capabilities beyond
file ownership and setuid for apt, no-new-privileges, the default seccomp profile, memory, CPU and
pid limits, no host mounts: /workspace is the volume `pos-sbx-ws-<agent id>` (kept across
restarts and resets), /shared the team's volume `pos-sbx-shared-<team>`. No PersonalOS secret
reaches a container: its environment is only the proxy, the locale and the api's address.

Capacity: at most SANDBOX_MAX_ACTIVE running at once; a new one stops the least recently used
idle container, or waits for one (SANDBOX_QUEUE_S). Idle containers stop after SANDBOX_IDLE_S
and start again, with the same files, on the next call.
"""

from __future__ import annotations

import base64
import io
import ipaddress
import json
import logging
import os
import posixpath
import re
import select
import shlex
import socket
import socketserver
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import docker
    from docker.errors import APIError, NotFound
except ImportError:  # the pure parts (proxy rules, paths) are tested without the Docker SDK
    docker = None

    class APIError(Exception):
        explanation = ""

    class NotFound(Exception):
        pass

log = logging.getLogger("sandbox")

IMAGE = os.environ.get("SANDBOX_IMAGE", "pos-agent-sandbox:latest")
NETWORK = os.environ.get("SANDBOX_NETWORK", "personalos_sandbox")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")
PROXY_HOST = os.environ.get("SANDBOX_PROXY_HOST", "sandbox-proxy")
MAX_ACTIVE = int(os.environ.get("SANDBOX_MAX_ACTIVE", "3"))
IDLE_S = int(os.environ.get("SANDBOX_IDLE_S", "900"))
QUEUE_S = int(os.environ.get("SANDBOX_QUEUE_S", "240"))
MEM = os.environ.get("SANDBOX_MEM", "2g")
CPUS = float(os.environ.get("SANDBOX_CPUS", "2"))
PIDS = int(os.environ.get("SANDBOX_PIDS", "512"))
QUOTA_MB = int(os.environ.get("SANDBOX_QUOTA_MB", "5120"))
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600
OUT_HEAD, OUT_TAIL = 6000, 6000
MAX_READ = 25 * 1024 * 1024
# Reachable through the proxy although private: the PersonalOS api, by name only.
ALLOW_PRIVATE = {(h.split(":")[0], int(h.split(":")[1])) for h in
                 os.environ.get("SANDBOX_ALLOW", "pos-api:8000").split(",") if ":" in h}
# Only the file-ownership and identity capabilities root needs for apt, pip and chown.
CAPS = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID", "KILL"]
LABEL = "pos.sandbox"

_lock = threading.Lock()
_inflight: dict[str, int] = {}
_last_used: dict[str, float] = {}


class Problem(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def client() -> docker.DockerClient:
    return docker.from_env(timeout=120)


def _slug(value) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    if not s:
        raise Problem(400, "bad name")
    return s[:40]


def cname(agent: str) -> str:
    return f"pos-sbx-{_slug(agent)}"


# ------------------------------------------------------------------ containers

def _running(c: docker.DockerClient) -> list:
    return c.containers.list(filters={"label": LABEL, "status": "running"})


def _create(c: docker.DockerClient, agent: str, team: str):
    env = {
        "HTTP_PROXY": f"http://{PROXY_HOST}:3128", "HTTPS_PROXY": f"http://{PROXY_HOST}:3128",
        "http_proxy": f"http://{PROXY_HOST}:3128", "https_proxy": f"http://{PROXY_HOST}:3128",
        "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
        "NPM_CONFIG_PROXY": f"http://{PROXY_HOST}:3128", "NPM_CONFIG_HTTPS_PROXY": f"http://{PROXY_HOST}:3128",
        "POS_API_URL": "http://pos-api:8000", "PIP_CACHE_DIR": "/tmp/pip-cache", "HOME": "/root",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_ROOT_USER_ACTION": "ignore",
    }
    return c.containers.create(
        IMAGE, name=cname(agent), hostname="sandbox", detach=True, init=False,
        labels={LABEL: "1", "pos.agent": _slug(agent), "pos.team": _slug(team)},
        environment=env, working_dir="/workspace",
        volumes={f"pos-sbx-ws-{_slug(agent)}": {"bind": "/workspace", "mode": "rw"},
                 f"pos-sbx-shared-{_slug(team)}": {"bind": "/shared", "mode": "rw"}},
        network=NETWORK, cap_drop=["ALL"], cap_add=CAPS, security_opt=["no-new-privileges:true"],
        mem_limit=MEM, memswap_limit=MEM, nano_cpus=int(CPUS * 1e9), pids_limit=PIDS,
        ulimits=[docker.types.Ulimit(name="nofile", soft=4096, hard=8192)],
        tmpfs={"/tmp": "size=1g", "/dev/shm": "size=256m"}, restart_policy={"Name": "no"},
        log_config=docker.types.LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
    )


def ensure(agent: str, team: str = "all"):
    """The agent's container, running. Starts it (creating it the first time) within the
    capacity: stops the least recently used idle one, or waits for one to free up."""
    c = client()
    name = cname(agent)
    try:
        box = c.containers.get(name)
    except NotFound:
        box = None
    if box is not None and box.status == "running":
        return box
    deadline = time.time() + QUEUE_S
    while True:
        with _lock:
            running = [r for r in _running(c) if r.name != name]
            if len(running) < MAX_ACTIVE:
                break
            idle = [r for r in running if not _inflight.get(r.name)]
            if idle:
                victim = min(idle, key=lambda r: _last_used.get(r.name, 0))
                log.info("capacity: stopping %s for %s", victim.name, name)
                victim.stop(timeout=5)
                break
        if time.time() > deadline:
            raise Problem(503, f"all {MAX_ACTIVE} sandboxes are busy running commands; try again in a few minutes")
        time.sleep(3)
    if box is None:
        box = _create(c, agent, team)
    box.start()
    box.reload()
    _last_used[name] = time.time()
    return box


class _Use:
    """Marks a container busy (never stopped for capacity while a call runs)."""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        with _lock:
            _inflight[self.name] = _inflight.get(self.name, 0) + 1
            _last_used[self.name] = time.time()

    def __exit__(self, *exc):
        with _lock:
            _inflight[self.name] = max(0, _inflight.get(self.name, 1) - 1)
            _last_used[self.name] = time.time()


def _exec(box, cmd: list[str], env: dict | None = None, workdir: str = "/workspace") -> tuple[int, bytes, bytes]:
    api = box.client.api
    ex = api.exec_create(box.id, cmd, environment=env or {}, workdir=workdir, user="root")
    out, err = api.exec_start(ex["Id"], demux=True)
    code = api.exec_inspect(ex["Id"]).get("ExitCode")
    return (code if code is not None else -1), out or b"", err or b""


def _clip(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= OUT_HEAD + OUT_TAIL:
        return text, False
    cut = len(text) - OUT_HEAD - OUT_TAIL
    return text[:OUT_HEAD] + f"\n… [{cut} characters cut: the full output is in the log file] …\n" + text[-OUT_TAIL:], True


def _abs(path: str) -> str:
    p = (path or ".").strip()
    if "\x00" in p:
        raise Problem(400, "bad path")
    return posixpath.normpath(p if p.startswith("/") else posixpath.join("/workspace", p))


def workspace_mb(box) -> int | None:
    code, out, _ = _exec(box, ["timeout", "20", "du", "-sxm", "/workspace"])
    try:
        return int(out.split()[0]) if code == 0 else None
    except (IndexError, ValueError):
        return None


# ------------------------------------------------------------------ operations

def op_exec(agent: str, team: str, command: str, timeout: int | None = None, workdir: str | None = None) -> dict:
    if not command or not command.strip():
        raise Problem(400, "empty command")
    t = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    box = ensure(agent, team)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    run = f"/tmp/.pos-exec/{stamp}"
    log_path = f"/workspace/.logs/exec-{stamp}.log"
    # The output goes to files, not the exec's pipe: a background process (a server started with &)
    # keeps no pipe open, so the call returns when the command does.
    script = (f'mkdir -p {run} /workspace/.logs; '
              f'timeout -k 10 {t} bash -lc "$POS_CMD" > {run}/out 2> {run}/err < /dev/null; echo $? > {run}/code; '
              f'{{ printf "$ %s\\n" "$POS_CMD"; cat {run}/out; echo "--- stderr"; cat {run}/err; '
              f'echo "--- exit $(cat {run}/code)"; }} > {log_path} 2>/dev/null; '
              f'ls -1t /workspace/.logs/exec-*.log 2>/dev/null | tail -n +101 | xargs -r rm -f')
    with _Use(box.name):
        started = time.time()
        _exec(box, ["bash", "-c", script], env={"POS_CMD": command}, workdir=_abs(workdir or "/workspace"))
        duration = round(time.time() - started, 1)
        files = _read_tar(box, run)
        _exec(box, ["rm", "-rf", run])
        used = workspace_mb(box)
    out, out_cut = _clip(files.get("out", b""))
    err, err_cut = _clip(files.get("err", b""))
    try:
        code = int(files.get("code", b"-1").strip() or -1)
    except ValueError:
        code = -1
    res = {"exit_code": code, "stdout": out, "stderr": err, "duration_s": duration,
           "timed_out": code in (124, 137) and duration >= t - 1, "log": log_path,
           "truncated": out_cut or err_cut, "workspace_mb": used}
    if res["timed_out"]:
        res["note"] = f"stopped after the {t} s limit (timeout up to {MAX_TIMEOUT})"
    if used is not None and used > QUOTA_MB:
        res["warning"] = (f"/workspace holds {used} MB, over the {QUOTA_MB} MB quota: delete files; "
                          "writing new files is refused until then")
    return res


def _read_tar(box, path: str) -> dict[str, bytes]:
    try:
        stream, _ = box.get_archive(path)
    except NotFound:
        return {}
    buf = io.BytesIO(b"".join(stream))
    out = {}
    with tarfile.open(fileobj=buf) as tar:
        for m in tar.getmembers():
            if m.isfile():
                out[os.path.basename(m.name)] = tar.extractfile(m).read()
    return out


def op_read(agent: str, team: str, path: str, max_bytes: int = 1024 * 1024) -> dict:
    p = _abs(path)
    box = ensure(agent, team)
    limit = max(1, min(int(max_bytes or 1024 * 1024), MAX_READ))
    with _Use(box.name):
        try:
            stream, stat = box.get_archive(p)
        except NotFound as e:
            raise Problem(404, f"no such file: {p}") from e
        if stat.get("mode", 0) & (1 << 31):  # a directory
            raise Problem(400, f"{p} is a directory: use sandbox_list")
        if stat.get("size", 0) > limit:
            raise Problem(413, f"{p} is {stat['size']} bytes, over the {limit} byte limit")
        buf = io.BytesIO(b"".join(stream))
    with tarfile.open(fileobj=buf) as tar:
        member = next((m for m in tar.getmembers() if m.isfile()), None)
        if member is None:
            raise Problem(400, f"{p} is not a regular file")
        data = tar.extractfile(member).read()
    return {"path": p, "size": len(data), "content_b64": base64.b64encode(data).decode()}


def op_write(agent: str, team: str, path: str, content_b64: str, mode: int = 0o644) -> dict:
    p = _abs(path)
    data = base64.b64decode(content_b64 or "")
    if len(data) > MAX_READ:
        raise Problem(413, f"at most {MAX_READ // (1024 * 1024)} MB per file")
    box = ensure(agent, team)
    with _Use(box.name):
        if p.startswith("/workspace"):
            used = workspace_mb(box)
            if used is not None and used > QUOTA_MB:
                raise Problem(507, f"/workspace holds {used} MB, over the {QUOTA_MB} MB quota: delete files first")
        d, name = posixpath.split(p)
        code, _, err = _exec(box, ["mkdir", "-p", d])
        if code != 0:
            raise Problem(400, f"cannot create {d}: {err.decode(errors='replace')[:200]}")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            tar.addfile(info, io.BytesIO(data))
        box.put_archive(d, buf.getvalue())
    return {"path": p, "size": len(data)}


def op_list(agent: str, team: str, path: str = "/workspace", depth: int = 2) -> dict:
    p = _abs(path)
    box = ensure(agent, team)
    d = max(1, min(int(depth or 2), 5))
    with _Use(box.name):
        code, out, err = _exec(box, ["bash", "-c", f"find {shlex.quote(p)} -mindepth 1 -maxdepth {d} "
                                     "-not -path '*/.logs/*' -not -path '*/node_modules/*' -not -path '*/.git/*' "
                                     "-printf '%y\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n' 2>&1 | sort -k4 | head -n 501"])
    if code != 0 and not out:
        raise Problem(404, err.decode(errors="replace")[:300] or f"cannot list {p}")
    entries = []
    for line in out.decode(errors="replace").splitlines()[:500]:
        parts = line.split("\t")
        if len(parts) == 4:
            kind = {"d": "dir", "f": "file", "l": "link"}.get(parts[0], parts[0])
            entries.append({"type": kind, "size": int(parts[1]) if parts[1].isdigit() else None,
                            "modified": parts[2], "path": parts[3]})
    return {"path": p, "entries": entries, "more": len(out.splitlines()) > 500}


def op_reset(agent: str, team: str, wipe: bool = False) -> dict:
    """A fresh container from the image (installed packages go); /workspace stays unless wipe."""
    c = client()
    name = cname(agent)
    with _lock:
        if _inflight.get(name):
            raise Problem(409, "a command is still running in this sandbox")
        try:
            c.containers.get(name).remove(force=True)
        except NotFound:
            pass
    if wipe:
        try:
            c.volumes.get(f"pos-sbx-ws-{_slug(agent)}").remove(force=True)
        except NotFound:
            pass
    return {"reset": True, "wiped": bool(wipe)}


def op_status() -> dict:
    c = client()
    out = []
    for box in c.containers.list(all=True, filters={"label": LABEL}):
        item = {"name": box.name, "agent": box.labels.get("pos.agent"), "status": box.status,
                "busy": bool(_inflight.get(box.name)), "last_used": _last_used.get(box.name)}
        if box.status == "running":
            try:
                st = box.stats(stream=False)
                item["mem_mb"] = round(st["memory_stats"].get("usage", 0) / 1048576)
            except (APIError, KeyError):
                pass
        out.append(item)
    return {"max_active": MAX_ACTIVE, "idle_stop_s": IDLE_S, "image": IMAGE, "containers": out}


def reaper() -> None:
    """Stop containers idle longer than IDLE_S (they start again, with their files, on the next call)."""
    while True:
        time.sleep(60)
        try:
            for box in _running(client()):
                last = _last_used.setdefault(box.name, time.time())
                if not _inflight.get(box.name) and time.time() - last > IDLE_S:
                    log.info("idle: stopping %s", box.name)
                    box.stop(timeout=5)
        except Exception:  # noqa: BLE001 - keep reaping
            log.exception("reaper")


# ------------------------------------------------------------------ control API

OPS = {"exec": op_exec, "read": op_read, "write": op_write, "list": op_list, "reset": op_reset}


def _sandbox_subnets() -> list:
    try:
        cfg = client().networks.get(NETWORK).attrs["IPAM"]["Config"] or []
        return [ipaddress.ip_network(x["Subnet"]) for x in cfg if x.get("Subnet")]
    except Exception:  # noqa: BLE001
        return []


class Control(BaseHTTPRequestHandler):
    server_version = "pos-sandbox"
    subnets: list = []

    def log_message(self, fmt, *args):
        log.info("control %s", fmt % args)

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self) -> bool:
        ip = ipaddress.ip_address(self.client_address[0])
        if any(ip in n for n in self.subnets):
            return False  # never from a sandbox, whatever it sends
        return bool(TOKEN) and self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if not self._allowed():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/status":
            return self._send(200, op_status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(401, {"error": "unauthorized"})
        op = OPS.get(self.path.strip("/"))
        if op is None:
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            agent = body.pop("agent")
            team = body.pop("team", None) or "all"
            self._send(200, op(agent, team, **body))
        except Problem as e:
            self._send(e.status, {"error": str(e)})
        except (KeyError, TypeError, ValueError) as e:
            self._send(400, {"error": f"bad request: {e}"})
        except APIError as e:
            log.exception("docker")
            self._send(502, {"error": f"docker: {e.explanation or e}"[:300]})


# ------------------------------------------------------------------ egress proxy

def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def resolve_allowed(host: str, port: int) -> tuple[str, int]:
    """The address to connect to, or PermissionError: public addresses only (all of them, checked
    after resolving, and connected by address so a second lookup cannot point elsewhere)."""
    host = host.strip("[]").lower()
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = [i[4][0] for i in infos]
    if not addrs:
        raise PermissionError(f"{host} does not resolve")
    if (host, port) in ALLOW_PRIVATE:
        return addrs[0], port
    if not all(_public(a) for a in addrs):
        raise PermissionError(f"{host} is on a private network; sandboxes reach only the internet")
    return addrs[0], port


def _relay(a: socket.socket, b: socket.socket, idle: int = 600) -> None:
    socks = [a, b]
    while True:
        r, _, x = select.select(socks, [], socks, idle)
        if x or not r:
            return
        for s in r:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            (b if s is a else a).sendall(data)


class Proxy(socketserver.StreamRequestHandler):
    def _deny(self, why: str, code: int = 403) -> None:
        msg = f"PersonalOS sandbox proxy: {why}\n".encode()
        self.wfile.write(f"HTTP/1.1 {code} Forbidden\r\nContent-Type: text/plain\r\nContent-Length: {len(msg)}\r\n"
                         f"Connection: close\r\n\r\n".encode() + msg)

    def handle(self):
        self.connection.settimeout(60)
        line = self.rfile.readline(8192).decode("latin-1")
        parts = line.split()
        if len(parts) != 3:
            return
        method, target, version = parts
        headers = []
        while True:
            h = self.rfile.readline(65536)
            if h in (b"\r\n", b"\n", b""):
                break
            headers.append(h)
        try:
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                addr = resolve_allowed(host, int(port))
                up = socket.create_connection(addr, timeout=30)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                log.info("proxy CONNECT %s:%s", host, port)
                self.connection.settimeout(None)
                up.settimeout(None)
                with up:
                    _relay(self.connection, up)
                return
            m = re.match(r"(?i)^http://([^/:]+|\[[^\]]+\])(?::(\d+))?(/.*)?$", target)
            if not m:
                return self._deny("only proxy requests (http://host/... or CONNECT)", 400)
            host, port, path = m.group(1), int(m.group(2) or 80), m.group(3) or "/"
            addr = resolve_allowed(host, port)
            up = socket.create_connection(addr, timeout=30)
            keep = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
            up.sendall(f"{method} {path} {version}\r\n".encode("latin-1") + b"".join(keep)
                       + b"Connection: close\r\n\r\n")
            log.info("proxy %s http://%s:%s", method, host, port)
            self.connection.settimeout(None)
            with up:
                _relay(self.connection, up)
        except PermissionError as e:
            log.info("proxy refused %s %s: %s", method, target, e)
            self._deny(str(e))
        except (OSError, ValueError) as e:
            self._deny(f"cannot connect: {e}", 502)


class ThreadingProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        log.warning("SANDBOX_TOKEN is empty: the control API refuses every call")
    Control.subnets = _sandbox_subnets()
    log.info("sandbox network %s: %s", NETWORK, [str(n) for n in Control.subnets])
    for box in client().containers.list(filters={"label": LABEL, "status": "running"}):
        _last_used[box.name] = time.time()  # adopt what runs (a manager restart)
    threading.Thread(target=reaper, daemon=True).start()
    proxy = ThreadingProxy(("0.0.0.0", 3128), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    log.info("proxy on :3128, control on :8200, image %s, at most %s active", IMAGE, MAX_ACTIVE)
    ThreadingHTTPServer(("0.0.0.0", 8200), Control).serve_forever()


if __name__ == "__main__":
    main()
