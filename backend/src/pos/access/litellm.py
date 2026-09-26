"""The seam for LiteLLM: per-agent virtual keys with their own budgets, and
spend read back from LiteLLM's admin API.

Off unless both POS_LITELLM_URL and POS_LITELLM_ADMIN_KEY are set; nothing in
PersonalOS calls the proxy automatically yet (the rollout is being planned
separately). When it is switched on, the intended wiring is:

- `upsert_key(agent_id, ...)` after a budget change for that agent
  (usd_day -> max_budget with budget_duration "1d", usd_month -> "30d"),
- `spend(agent_id)` in pos.access.service.usage (already read when enabled),
- the worker gets the agent's virtual key instead of the shared one.

Keys are addressed by alias `pos-agent-<id>`, so PersonalOS never stores them.
"""

import os
from dataclasses import dataclass

import httpx


def _alias(agent_id: int) -> str:
    return f"pos-agent-{agent_id}"


@dataclass
class LiteLLMAdmin:
    url: str
    admin_key: str
    transport: httpx.BaseTransport | None = None  # tests pass httpx.MockTransport
    timeout: float = 10.0

    enabled = True

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url.rstrip("/"), timeout=self.timeout, transport=self.transport,
                            headers={"Authorization": f"Bearer {self.admin_key}"})

    def upsert_key(self, agent_id: int, *, max_budget_usd: float | None, budget_duration: str | None = "1d",
                   models: list[str] | None = None, metadata: dict | None = None) -> dict:
        """Create the agent's virtual key, or update its budget when it exists."""
        body = {"key_alias": _alias(agent_id), "max_budget": max_budget_usd, "budget_duration": budget_duration,
                "metadata": {"pos_agent_id": agent_id, **(metadata or {})}}
        if models:
            body["models"] = models
        with self._client() as c:
            info = c.get("/key/info", params={"key_alias": _alias(agent_id)})
            if info.status_code == 200 and (info.json() or {}).get("info"):
                key = info.json().get("key")
                r = c.post("/key/update", json={**body, "key": key})
            else:
                r = c.post("/key/generate", json=body)
            r.raise_for_status()
            out = r.json()
            out.pop("key", None)  # the secret never goes into PersonalOS
            return out

    def spend(self, agent_id: int) -> dict | None:
        """{"spend": USD, "max_budget": ..., "budget_reset_at": ...} for the agent's key, or None."""
        try:
            with self._client() as c:
                r = c.get("/key/info", params={"key_alias": _alias(agent_id)})
                if r.status_code != 200:
                    return None
                info = (r.json() or {}).get("info") or {}
        except httpx.HTTPError:
            return None
        return {k: info.get(k) for k in ("spend", "max_budget", "budget_duration", "budget_reset_at")}


class Disabled:
    enabled = False

    def upsert_key(self, *_a, **_k) -> None:
        return None

    def spend(self, *_a, **_k) -> None:
        return None


def client(transport: httpx.BaseTransport | None = None) -> "LiteLLMAdmin | Disabled":
    url, key = os.environ.get("POS_LITELLM_URL", "").strip(), os.environ.get("POS_LITELLM_ADMIN_KEY", "").strip()
    if not (url and key):
        return Disabled()
    return LiteLLMAdmin(url, key, transport=transport)
