#!/usr/bin/env python3
"""The svr03 side of the SRE's runbook (pos.ops_runbook): a tiny unix-socket server.

Runs as drosko's systemd --user service (pos-ops-runbook.service). The PersonalOS API
container reaches it through a socket in a directory bind-mounted into the container
(POS_OPS_RUNBOOK_SOCKET). One JSON request line in, one JSON answer out:

    {"v": 1, "action": "docker_logs", "params": {"container": "personalos-api-1"},
     "reason": "...", "request_id": "..."}
    -> {"ok": true, "exit_code": 0, "output": "...", "duration_ms": 120}

Defence in depth: it never trusts the API. It accepts only an action name and parameters
(never a command), validates them again with its own frozen copy of the catalogue
(catalogue.py = backend/src/pos/ops_runbook.py at install time, so a later commit to the
checkout changes nothing until the owner reinstalls), builds the argv itself, checks the
program against a short list, and runs it without a shell, with a timeout and a minimal
environment. Only peers with an allowed uid may connect (SO_PEERCRED). Write actions run one
at a time. Every request is logged to the journal (journalctl --user -u pos-ops-runbook).

Stdlib only (python3 of the host).
"""

import argparse
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import catalogue  # noqa: E402  (the frozen copy of pos.ops_runbook)

PROGRAMS = {"docker", "df", "free", "uptime", "systemctl", "journalctl", "systemd-run"}
REQUEST_KEYS = {"v", "action", "params", "reason", "request_id"}
MAX_REQUEST = 16_384
MAX_OUTPUT = 65_536
ENV_KEYS = ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
_write_lock = threading.Lock()


class Refused(Exception):
    pass


def _env() -> dict:
    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    env.update({"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "SYSTEMD_PAGER": "", "PAGER": "cat", "NO_COLOR": "1"})
    return env


def subprocess_runner(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, env=_env(), cwd="/", check=False)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace")
        return 124, out + f"\n[timed out after {timeout:.0f} s]"
    return p.returncode, p.stdout.decode("utf-8", "replace")


def write_known_hosts(path: str, text: str) -> tuple[int, str]:
    target = Path(path)
    if not target.parent.is_dir():
        return 1, f"{target.parent} does not exist"
    old = target.read_text() if target.exists() else None
    if old == text:
        return 0, f"{path}: already the pinned keys"
    if old is not None:
        (target.parent / (target.name + ".bak")).write_text(old)
    tmp = target.parent / f".{target.name}.tmp-{os.getpid()}"
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return 0, f"{path}: wrote {len(catalogue.PINNED_KNOWN_HOSTS)} pinned GitHub host keys" + (
        f" (previous file kept as {target.name}.bak)" if old is not None else "")


def handle(request, runner=subprocess_runner, known_hosts_writer=write_known_hosts) -> dict:
    """Validate one request again and run it. Never raises."""
    started = time.monotonic()
    try:
        if not isinstance(request, dict):
            raise Refused("the request must be a JSON object")
        extra = set(request) - REQUEST_KEYS
        if extra:
            raise Refused(f"unexpected fields: {', '.join(sorted(map(str, extra)))[:200]}")
        if request.get("v") != catalogue.PROTOCOL:
            raise Refused("unsupported protocol version")
        reason = request.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise Refused("a reason is required")
        action = request.get("action")
        try:
            params = catalogue.validate(action, request.get("params"))
        except catalogue.Invalid as e:
            raise Refused(str(e)) from e
        plan = catalogue.plan(action, params)
        write = catalogue.ACTIONS[action]["kind"] == "write"
        if "known_hosts" in plan:
            if plan["known_hosts"] not in catalogue.DEPLOYERS.values():
                raise Refused("not a deployer's known_hosts")
            with _write_lock:
                code, out = known_hosts_writer(plan["known_hosts"], catalogue.known_hosts_text())
        else:
            argv = plan["argv"]
            if not argv or argv[0] not in PROGRAMS or any(not isinstance(a, str) or "\x00" in a for a in argv):
                raise Refused("not an allowed program")
            if write:
                with _write_lock:
                    code, out = runner(argv, plan["timeout"])
            else:
                code, out = runner(argv, plan["timeout"])
    except Refused as e:
        return {"ok": False, "exit_code": None, "error": f"refused: {e}", "output": ""}
    except Exception as e:  # noqa: BLE001 - one bad request never stops the executor
        return {"ok": False, "exit_code": None, "error": f"{type(e).__name__}: {str(e)[:300]}", "output": ""}
    if len(out) > MAX_OUTPUT:
        out = out[-MAX_OUTPUT:] if action in catalogue.LOG_ACTIONS else out[:MAX_OUTPUT]
    return {"ok": code == 0, "exit_code": code, "output": out,
            "duration_ms": int((time.monotonic() - started) * 1000)}


def peer_uid(sock) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except (OSError, AttributeError):
        return None


def log(**fields) -> None:
    print(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}, ensure_ascii=False), flush=True)


class Handler(socketserver.StreamRequestHandler):
    allowed_uids: set[int] = set()

    def handle(self) -> None:
        uid = peer_uid(self.connection)
        if uid not in self.allowed_uids:
            log(event="refused_peer", uid=uid)
            self._answer({"ok": False, "exit_code": None, "error": "refused: peer not allowed", "output": ""})
            return
        line = self.rfile.readline(MAX_REQUEST + 1)
        if len(line) > MAX_REQUEST:
            self._answer({"ok": False, "exit_code": None, "error": "refused: request too long", "output": ""})
            return
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            request = None
        answer = handle(request)
        req = request if isinstance(request, dict) else {}
        log(event="run", uid=uid, action=str(req.get("action"))[:40], params=req.get("params"),
            reason=str(req.get("reason") or "")[:200], request_id=req.get("request_id"), ok=answer["ok"],
            exit_code=answer.get("exit_code"), error=answer.get("error"), output_len=len(answer.get("output") or ""))
        self._answer(answer)

    def _answer(self, answer: dict) -> None:
        try:
            self.wfile.write(json.dumps(answer).encode() + b"\n")
        except OSError:
            pass


def main() -> None:
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):  # Linux only (svr03)
        daemon_threads = True

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--socket", required=True)
    ap.add_argument("--allow-uid", action="append", type=int, default=None,
                    help="peer uids allowed to connect (default: 0 = the API container's root, and our own uid)")
    args = ap.parse_args()
    Handler.allowed_uids = set(args.allow_uid or [0, os.getuid()])
    path = Path(args.socket)
    if path.exists() or path.is_socket():
        path.unlink()
    old = os.umask(0o117)  # the socket: rw for drosko and its group only
    try:
        server = Server(str(path), Handler)
    finally:
        os.umask(old)
    catalogue.known_hosts_text()  # a pinned key that does not match its fingerprint stops the start
    log(event="start", socket=str(path), allowed_uids=sorted(Handler.allowed_uids),
        actions=sorted(catalogue.ACTIONS))
    try:
        server.serve_forever()
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
