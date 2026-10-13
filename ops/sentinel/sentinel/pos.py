"""Talking to PersonalOS: incident events (/api/events, idempotent by ref),
the heartbeat and PersonalOS's own run numbers. What PersonalOS does not take
waits in the outbox and is retried every tick; while PersonalOS is down the
fallback file and /fallback say so (see api.py)."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .store import Store

ALERT_PATH = "/api/sentinel/alert"


def _answer(e: HTTPError) -> dict:
    try:
        out = json.loads(e.read() or b"{}")
        return out if isinstance(out, dict) else {}
    except (ValueError, OSError):
        return {}


class Pos:
    def __init__(self, url: str, token: str, store: Store, timeout: float = 10.0):
        self.url, self.token, self.s, self.timeout = url.rstrip("/"), token, store, timeout
        self.last_ok: float | None = None
        self.last_error: str = ""

    def _post(self, path: str, payload: dict) -> dict:
        req = Request(self.url + path, data=json.dumps(payload).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with urlopen(req, timeout=self.timeout) as r:
            out = json.loads(r.read() or b"{}")
        self.last_ok = time.time()
        return out

    def send(self, path: str, payload: dict) -> dict | None:
        """POST now; on failure keep it in the outbox (events only). Events keep
        their order: while older ones wait, a new one queues behind them."""
        if path == "/api/events" and self.pending():
            self.s.x("INSERT INTO outbox (path, payload, created_at) VALUES (?, ?, ?)", path, json.dumps(payload), time.time())
            self.flush()
            return None
        try:
            return self._post(path, payload)
        except HTTPError as e:
            self.last_error = f"HTTP {e.code}"
            if e.code < 500 and e.code not in (401, 408, 429):
                return {"error": self.last_error}  # a bad event would never succeed: drop it
        except (URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
        if path == "/api/events":
            self.s.x("INSERT INTO outbox (path, payload, created_at) VALUES (?, ?, ?)", path, json.dumps(payload), time.time())
        return None

    def alert(self, payload: dict) -> dict | None:
        """A platform alert (POST /api/sentinel/alert: the owner and the SRE, sentinel.platform). Kept in the
        outbox until PersonalOS records it; when it answers 503 after the owner already has it, the retry says
        owner_done so he is not told twice."""
        try:
            return self._post(ALERT_PATH, payload)
        except HTTPError as e:
            self.last_error = f"HTTP {e.code}"
            answer = _answer(e)
            if e.code < 500 and e.code not in (401, 408, 429):
                return {"error": self.last_error, **answer}
            if answer.get("owner_done"):
                payload = {**payload, "owner_done": True}
        except (URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            answer = {}
        self.s.x("INSERT INTO outbox (path, payload, created_at) VALUES (?, ?, ?)", ALERT_PATH, json.dumps(payload),
                 time.time())
        return {"queued": True, **answer}

    def flush(self, limit: int = 20) -> int:
        """Retry the outbox, platform alerts first, then events oldest first; stops at the first failure."""
        sent = 0
        for r in self.s.q("SELECT * FROM outbox ORDER BY (path = ?) DESC, id LIMIT ?", ALERT_PATH, limit):
            try:
                self._post(r["path"], json.loads(r["payload"]))
            except HTTPError as e:
                if e.code < 500 and e.code not in (401, 408, 429):
                    self.s.x("DELETE FROM outbox WHERE id = ?", r["id"])
                    continue
                if r["path"] == ALERT_PATH and _answer(e).get("owner_done"):
                    self.s.x("UPDATE outbox SET payload = ? WHERE id = ?",
                             json.dumps({**json.loads(r["payload"]), "owner_done": True}), r["id"])
                self.s.x("UPDATE outbox SET tries = tries + 1, last_error = ? WHERE id = ?", f"HTTP {e.code}", r["id"])
                break
            except (URLError, OSError, ValueError) as e:
                self.s.x("UPDATE outbox SET tries = tries + 1, last_error = ? WHERE id = ?", str(e)[:200], r["id"])
                break
            self.s.x("DELETE FROM outbox WHERE id = ?", r["id"])
            sent += 1
        return sent

    def pending(self) -> int:
        return self.s.one("SELECT COUNT(*) AS n FROM outbox")["n"]

    def heartbeat(self, payload: dict) -> bool:
        try:
            self._post("/api/sentinel/heartbeat", payload)
            return True
        except (HTTPError, URLError, OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            return False
