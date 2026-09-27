"""HTTP client for the PersonalOS worker API (/api/worker/*), bearer-authenticated."""

import httpx


class Blocked(Exception):
    """PersonalOS refused to start a run (kill switch, pause, budget)."""


class PosClient:
    def __init__(self, base_url: str, key: str, http: httpx.Client | None = None, timeout: float = 150):
        self.http = http or httpx.Client(base_url=base_url, timeout=timeout)
        self.http.headers["Authorization"] = f"Bearer {key}"

    def _get(self, path: str, **params):
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None):
        r = self.http.post(path, json=body or {})
        if r.status_code == 409:
            raise Blocked(r.json().get("detail", "blocked"))
        r.raise_for_status()
        return r.json()

    def feedback(self) -> list[dict]:
        """Open feedback for this agent (empty on an older PersonalOS)."""
        try:
            return self._get("/api/worker/feedback")
        except Exception:  # noqa: BLE001 - feedback is context, never a reason not to work
            return []

    def memory(self) -> str:
        """The agent's pinned memory (empty on an older PersonalOS)."""
        try:
            return str((self._get("/api/worker/memory") or {}).get("body") or "")
        except Exception:  # noqa: BLE001 - memory is context, never a reason not to work
            return ""

    def me(self) -> dict:
        return self._get("/api/worker/me")

    def next_work(self, wait: int = 60) -> dict:
        return self._get("/api/worker/next", wait=wait)

    def task(self, ref: str) -> dict:
        return self._get(f"/api/worker/tasks/{ref}")

    def claim(self, ref: str, run_id: int | None = None) -> dict:
        return self._post(f"/api/worker/tasks/{ref}/claim" + (f"?run_id={run_id}" if run_id else ""))

    def complete(self, ref: str, note: str) -> dict:
        return self._post(f"/api/worker/tasks/{ref}/complete", {"note": note})

    def progress(self, ref: str, percent: int, message: str) -> dict:
        return self._post(f"/api/worker/tasks/{ref}/progress", {"percent": percent, "message": message})

    def handback(self, ref: str, note: str) -> dict:
        return self._post(f"/api/worker/tasks/{ref}/handback", {"note": note})

    def triage(self, ref: str, verdict: dict) -> dict:
        """What the cheap check said; PersonalOS parks or hands back the task, or lets it run."""
        return self._post(f"/api/worker/tasks/{ref}/triage", verdict)

    def start_run(self, ref: str | None, kind: str = "task") -> dict:
        """{"run_id", "engine", "model"}: PersonalOS picks the runtime (Claude or Codex)."""
        return self._post("/api/worker/runs", {"task_id": ref, "kind": kind})

    def heartbeat(self, run_id: int, step: str | None = None, steps: int | None = None) -> dict:
        """A completed step: the run's state back; `step` (what it was) feeds the chat status snapshot."""
        body = {k: v for k, v in (("step", step), ("steps", steps)) if v is not None}
        return self._post(f"/api/worker/runs/{run_id}/heartbeat", body)

    def alive(self, run_id: int) -> None:
        """The tick between steps: keeps a chat run's "working" indicator (never raises)."""
        try:
            self.http.post(f"/api/worker/runs/{run_id}/alive", json={})
        except Exception:  # noqa: BLE001 - an older PersonalOS, or a blip: only the indicator suffers
            pass

    def finish_run(self, run_id: int, status: str, jsonl: str, detail: str = "") -> dict:
        return self._post(f"/api/worker/runs/{run_id}/finish", {"status": status, "jsonl": jsonl, "detail": detail})

    def inbox(self, run_id: int | None = None) -> list[dict]:
        return self._get("/api/worker/inbox", **({"run_id": run_id} if run_id else {}))

    def tools(self) -> list[dict]:
        """The agent's tools from the tool library (personal and shared)."""
        return self._get("/api/worker/tools")

    def check_command(self, command: str, external: bool = False) -> dict:
        return self._post("/api/worker/check-command", {"command": command, "external": external})

    def credential_session(self, run_id: int) -> str:
        """A token for this run's credential runner (pos_worker.credentials)."""
        return self._post("/api/worker/credentials/session", {"run_id": run_id})["token"]

    def wrap(self, source: str, content: str, ref: str | None = None) -> str:
        return self._post("/api/worker/wrap", {"source": source, "content": content, "ref": ref})["wrapped"]
