"""The credential runner: run one command with a secret the agent never sees.

A stdio MCP server the worker mounts as `credentials` for agents that hold a
credential grant (`cred:<name>`, pos.credentials). Its one tool,
`run_with_credentials`, takes a command with `{{cred:<name>}}` placeholders
(or names in `credentials`) and:

1. asks PersonalOS's command guard about the command as written (placeholders,
   no values), like every Bash command;
2. asks PersonalOS for the values (the agent's key plus this run's session
   token): the grant, the credential's allowed commands and hosts and the
   hourly limit are checked there, and the use is logged;
3. runs the command with each value in its env var for that one subprocess;
   a placeholder becomes a reference to the variable (`${GITHUB_TOKEN}`), so the
   value is not even in the command line;
4. returns the output with every value (plain, base64, URL-encoded, hex, also
   split across chunks) replaced by [REDACTED:<name>].

The values live only in this process's memory for the duration of the call.
Nothing is cached or written here (PersonalOS keeps a short in-memory cache).

Environment: POS_URL, POS_AGENT_KEY, POS_CRED_SESSION (the run's token),
POS_RUN_ID, WORKER_WORKDIR.
"""

import codecs
import os
import re
import subprocess
import sys
import threading
import time

import httpx

from .redact import Redactor

PLACEHOLDER = re.compile(r"\{\{\s*cred:([a-z0-9][a-z0-9_.-]*)\s*\}\}")
MAX_OUTPUT = 30_000
# Never handed to the command: the platform's own keys.
STRIP_ENV = ("POS_AGENT_KEY", "POS_CRED_SESSION", "OP_SERVICE_ACCOUNT_TOKEN", "POS_MCP_TOKEN")


def env_ref(var: str, windows: bool | None = None) -> str:
    windows = sys.platform == "win32" if windows is None else windows
    return f"%{var}%" if windows else "${" + var + "}"


class Runner:
    def __init__(self, post=None, env: dict | None = None):
        self.env = dict(os.environ if env is None else env)
        self._post = post  # tests: (path, body, headers) -> (status, json)

    def post(self, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
        if self._post is not None:
            return self._post(path, body, headers or {})
        r = httpx.post(self.env.get("POS_URL", "http://localhost:8000").rstrip("/") + path, json=body, timeout=60,
                       headers={"Authorization": f"Bearer {self.env.get('POS_AGENT_KEY', '')}", **(headers or {})})
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        return r.status_code, data

    def run(self, command: str, credentials: list[str] | None = None, timeout_s: int = 300) -> dict:
        command = (command or "").strip()
        if not command:
            return {"ok": False, "error": "no command"}
        names = sorted({str(n).strip().lower() for n in (credentials or []) if str(n).strip()}
                       | set(PLACEHOLDER.findall(command)))
        if not names:
            return {"ok": False, "error": "name a credential ({{cred:<name>}} or credentials=[...]); "
                                          "commands without one go through Bash"}
        run_id = int(self.env.get("POS_RUN_ID") or 0)
        # 1. The command guard sees the command as the agent wrote it.
        try:
            code, verdict = self.post("/api/worker/check-command", {"command": command, "run_id": run_id or None})
        except Exception as e:  # noqa: BLE001 - no guard, no command
            return {"ok": False, "error": f"the command guard is not reachable ({type(e).__name__}); not run"}
        if code != 200 or verdict.get("outcome") != "allow":
            return {"ok": False, "error": verdict.get("reason") or f"refused by the command guard ({code})",
                    **({"owner_task": verdict["owner_task"]} if verdict.get("owner_task") else {})}
        # 2. The values, for this command only (checked and logged by PersonalOS).
        try:
            code, got = self.post("/api/worker/credentials/resolve",
                                  {"run_id": run_id, "names": names, "command": command, "tool": "command"},
                                  {"X-POS-Cred-Session": self.env.get("POS_CRED_SESSION", "")})
        except Exception as e:  # noqa: BLE001 - fail closed
            return {"ok": False, "error": f"PersonalOS is not reachable ({type(e).__name__}); not run"}
        if code != 200:
            return {"ok": False, "error": f"credential refused: {got.get('detail') or code}"}
        creds = got.get("credentials") or {}
        if set(creds) != set(names):
            return {"ok": False, "error": "credential refused: not every credential was resolved"}
        red = Redactor({n: v["value"] for n, v in creds.items()})
        env = {k: v for k, v in self.env.items() if k not in STRIP_ENV}
        for v in creds.values():
            env[v["env_var"]] = v["value"]
        final = PLACEHOLDER.sub(lambda m: env_ref(creds[m.group(1)]["env_var"]), command)
        try:
            return self._exec(final, env, red, timeout_s, names)
        finally:
            creds.clear()
            env.clear()

    def _exec(self, command: str, env: dict, red: Redactor, timeout_s: int, names: list[str]) -> dict:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        proc = subprocess.Popen(command, shell=True, cwd=self.env.get("WORKER_WORKDIR") or None, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        stream = red.stream()
        # An incremental UTF-8 decoder so a multibyte character (and a secret) split across two
        # byte chunks is not mangled at the boundary; the redactor's Stream then holds back a
        # tail so a secret split across character chunks is still redacted.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        out: list[str] = []
        size = 0
        truncated = False

        def emit(text: str) -> None:
            nonlocal size, truncated
            if not text:
                return
            safe = stream.feed(text)
            if not safe:
                return
            if size < MAX_OUTPUT:
                out.append(safe)
                size += len(safe)
            else:
                truncated = True

        def pump():
            assert proc.stdout
            while True:
                chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    emit(decoder.decode(b"", final=True))  # flush any bytes held for a split character
                    break
                emit(decoder.decode(chunk))

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        deadline = time.monotonic() + max(1, min(int(timeout_s or 300), 1800))
        timed_out = False
        while proc.poll() is None:
            if time.monotonic() > deadline:
                timed_out = True
                from .codex import kill_tree

                kill_tree(proc.pid)
                break
            time.sleep(0.05)
        proc.wait(timeout=10)
        t.join(timeout=5)
        out.append(stream.close())
        text = "".join(out)
        if len(text) > MAX_OUTPUT:
            text, truncated = text[:MAX_OUTPUT], True
        return {"ok": proc.returncode == 0 and not timed_out, "exit_code": proc.returncode, "output": text,
                "truncated": truncated, "timed_out": timed_out, "credentials": names}


def main() -> None:
    from mcp.server.mcpserver import MCPServer

    runner = Runner()
    server = MCPServer("credentials", instructions=(
        "Run a command that needs a secret without seeing it: put {{cred:<name>}} where it goes (or name it in "
        "credentials, then it is in its env var). The output comes back with the value redacted. "
        "credentials_list (pos) shows the names you hold."))

    @server.tool(description="Run one command with credentials you hold. Write {{cred:<name>}} where the secret goes "
                             "(e.g. git push https://x-access-token:{{cred:github-deploy}}@github.com/org/repo.git), "
                             "or list names in credentials and use their env var. One plain command (no ; | & > <), "
                             "within the credential's allowed commands and hosts; checked by the command guard. "
                             "The value is never shown to you; output has it redacted.")
    def run_with_credentials(command: str, credentials: list[str] | None = None, timeout_s: int = 300) -> dict:
        return runner.run(command, credentials, timeout_s)

    server.run()


if __name__ == "__main__":
    main()
