"""Every agent's own computer (docs/SANDBOX.md): the api's side of the sandbox manager.

The manager (ops/sandbox/manager.py, the `sandbox` service) runs one container per agent; the
pos MCP tools sandbox_* call it here, as the agent that called them (its id names the container,
its team picks the /shared volume). The calls are async: a command may run for up to an hour and
must not hold a thread or a database transaction meanwhile.

Settings: POS_SANDBOX_URL (default http://pos-sandbox:8200), POS_SANDBOX_TOKEN (shared with the
manager, in .env). Without the token the tools say the sandbox is not set up.
"""

import base64
import os
import re
import sqlite3
import time

import httpx

from . import actors

DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600
_transport: httpx.AsyncBaseTransport | None = None  # tests put a mock transport here


class SandboxError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("POS_SANDBOX_TOKEN"))


def identity(conn: sqlite3.Connection, actor_id: int) -> dict:
    """Which container and which /shared volume: the member's id, and its team (else "all")."""
    row = actors.get(conn, actor_id)
    team = (row["team"] if "team" in row.keys() else None) or "all"
    return {"agent": str(actor_id), "team": re.sub(r"[^a-z0-9-]+", "-", team.lower()).strip("-") or "all"}


async def call(op: str, who: dict, *, wait: float = 120, **body) -> dict:
    if not configured():
        raise SandboxError("the sandbox is not set up on this PersonalOS (POS_SANDBOX_TOKEN)")
    url = os.environ.get("POS_SANDBOX_URL", "http://pos-sandbox:8200").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['POS_SANDBOX_TOKEN']}"}
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=wait + 300) as client:
            r = await client.post(f"{url}/{op}", json={**who, **body}, headers=headers)
    except httpx.HTTPError as e:
        raise SandboxError(f"the sandbox manager is unreachable: {e}"[:300]) from e
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:300]}
    if r.status_code >= 400:
        raise SandboxError(data.get("error") or f"sandbox answered {r.status_code}")
    return data


async def run(who: dict, command: str, timeout: int | None = None, workdir: str | None = None) -> dict:
    t = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    return await call("exec", who, wait=t, command=command, timeout=t, workdir=workdir)


async def write(who: dict, path: str, data: bytes) -> dict:
    return await call("write", who, path=path, content_b64=base64.b64encode(data).decode())


async def read(who: dict, path: str, max_bytes: int = 1024 * 1024) -> tuple[bytes, str]:
    out = await call("read", who, path=path, max_bytes=max_bytes)
    return base64.b64decode(out["content_b64"]), out["path"]


async def run_python(who: dict, code: str, timeout: int | None = None) -> dict:
    path = f"/workspace/.runs/run-{time.strftime('%Y%m%d-%H%M%S')}.py"
    await write(who, path, code.encode("utf-8"))
    out = await run(who, f"python3 {path}", timeout)
    out["script"] = path
    return out


def as_text(data: bytes, limit: int) -> dict:
    """File content for the model: text when it is text, else a note (binary is shared, not read)."""
    if b"\x00" in data[:8192]:
        return {"binary": True, "size": len(data),
                "note": "binary file: share it with sandbox_share, or inspect it with sandbox_exec (file, head)"}
    text = data.decode("utf-8", errors="replace")
    return {"content": text[:limit], "size": len(data), **({"truncated": True} if len(text) > limit else {})}


# ------------------------------------------------------------------ the isolation check (on the server)

# (what, command, passes when) — run in a throwaway sandbox ("check" agent) by `python -m pos.sandbox check`.
LAN = ("192.168.1.1", "192.168.1.108", "192.168.1.186", "10.66.66.1", "172.17.0.1", "169.254.169.254")
CHECKS = [
    ("no PersonalOS secret in the environment",
     "env | cut -d= -f1 | sort | tr '\\n' ' '",
     lambda r: not re.search(r"TOKEN|SECRET|PASSWORD|_KEY\b|OAUTH|VAPID|CLAUDE|OPENAI", r["stdout"])),
    ("no Docker socket, no host paths mounted",
     "test -e /var/run/docker.sock && echo SOCK; awk '{print $2}' /proc/mounts | grep -vE "
     "'^/(proc|sys|dev|tmp|workspace|shared|etc/(hosts|hostname|resolv.conf))?(/|$)' | head -5",
     lambda r: "SOCK" not in r["stdout"] and not r["stdout"].strip()),
    ("capabilities are only file ownership, setuid/setgid and kill",
     "grep CapEff /proc/self/status | awk '{print $2}'",
     lambda r: int(r["stdout"].strip() or "0", 16) & ~((1 << 0) | (1 << 1) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6)
                                                    | (1 << 7)) == 0),
    ("memory and pid limits",
     "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max",
     lambda r: r["stdout"].split()[:2] == ["2147483648", "512"]),
    ("the LAN is unreachable through the proxy",
     " ; ".join(f"curl -s -o /dev/null -w '{h}:%{{http_code}} ' -m 5 http://{h}/" for h in LAN),
     lambda r: all(f"{h}:000" in r["stdout"] or f"{h}:403" in r["stdout"] for h in LAN)),
    ("no route out without the proxy",
     "curl -s -o /dev/null -w '%{http_code}' -m 5 --noproxy '*' https://1.1.1.1/ || echo BLOCKED",
     lambda r: "BLOCKED" in r["stdout"] or r["stdout"].strip().startswith("000")),
    ("the control API refuses a sandbox",
     "curl -s -m 5 --noproxy '*' http://sandbox-proxy:8200/status; echo; curl -s -m 5 http://pos-sandbox:8200/status",
     lambda r: '"containers"' not in r["stdout"]),
    ("the internet works through the proxy (PyPI over HTTPS)",
     "curl -s -o /dev/null -w '%{http_code}' -m 20 https://pypi.org/simple/pip/",
     lambda r: r["stdout"].strip() == "200"),
    ("the PersonalOS api is reachable",
     "curl -s -m 10 http://pos-api:8000/api/health",
     lambda r: '"ok"' in r["stdout"] or "ok" in r["stdout"].lower()),
    ("a command over its time limit is stopped",
     "sleep 30", lambda r: r.get("timed_out") is True),
]


async def check(agent: str = "isolation-check") -> list[tuple[str, bool, str]]:
    who = {"agent": agent, "team": "check"}
    out = []
    for name, cmd, ok in CHECKS:
        try:
            r = await run(who, cmd, timeout=3 if cmd == "sleep 30" else 60)
            passed = bool(ok(r))
            detail = (r.get("stdout", "") + r.get("stderr", ""))[:300]
        except Exception as e:  # noqa: BLE001 - report every check
            passed, detail = False, str(e)[:300]
        out.append((name, passed, detail))
    # /workspace survives a reset (a fresh container from the image)
    try:
        await write(who, "keep.txt", b"kept")
        await call("reset", who)
        data, _ = await read(who, "keep.txt")
        out.append(("/workspace survives a reset", data == b"kept", ""))
        await call("reset", who, wipe=True)
    except Exception as e:  # noqa: BLE001
        out.append(("/workspace survives a reset", False, str(e)[:300]))
    return out


def main(argv: list[str] | None = None) -> int:
    import asyncio
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["check"]:
        print("usage: python -m pos.sandbox check")
        return 2
    results = asyncio.run(check())
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f"\n      {detail}"))
    return 0 if all(p for _, p, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
