"""The 1Password seam: a service account through the official Python SDK.

Configuration (the api container's environment, never the database):

    OP_SERVICE_ACCOUNT_TOKEN   the service account token (ops_…)
    POS_OP_VAULT               the one vault agents' credentials live in ("PersonalOS Agents")
    POS_OP_CACHE_SECONDS       how long a resolved value stays in memory (default 300, at most 300)

Without the token or the vault the feature is off and every use fails closed.
Values are kept in this process's memory only (a dict with an expiry), never
written to disk, logs or the database. Listing the vault returns metadata:
item titles, categories and field names/types, never field values.

The SDK (`onepassword-sdk`) is async and needs glibc >= 2.32 and OpenSSL 3
(python:3.12-slim is fine). It runs on one background event loop here, so
sync code (FastAPI handlers, MCP tools, tests) can call it from any thread.
Tests swap the provider with `set_provider`.
"""

import asyncio
import os
import threading
import time
from dataclasses import dataclass

INTEGRATION = ("PersonalOS", "1.0.0")
TIMEOUT_S = 20


class Unavailable(RuntimeError):
    """1Password is not configured or cannot be reached: fail closed."""


def configured() -> tuple[bool, str]:
    """(on, why not)."""
    if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "").strip():
        return False, "OP_SERVICE_ACCOUNT_TOKEN is not set on the server"
    if not vault():
        return False, "POS_OP_VAULT is not set on the server"
    return True, ""


def vault() -> str:
    return os.environ.get("POS_OP_VAULT", "").strip()


def cache_seconds() -> float:
    try:
        return max(0.0, min(300.0, float(os.environ.get("POS_OP_CACHE_SECONDS", "300"))))
    except ValueError:
        return 300.0


@dataclass
class FieldMeta:
    id: str
    title: str
    type: str
    section: str | None


class SdkProvider:
    """The real thing: onepassword-sdk with the service account token."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._lock = threading.Lock()

    def _run(self, coro_fn):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="onepassword", daemon=True).start()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
        try:
            return fut.result(TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - every failure is 'unavailable'; never echo a value
            self._client = None  # a new login next time (an expired session, a network hiccup)
            raise Unavailable(f"1Password: {type(e).__name__}: {str(e)[:200]}") from None

    async def _get_client(self):
        if self._client is None:
            try:
                from onepassword.client import Client
            except ImportError as e:
                raise Unavailable("the onepassword-sdk package is not installed in the api image") from e
            self._client = await Client.authenticate(auth=os.environ["OP_SERVICE_ACCOUNT_TOKEN"],
                                                     integration_name=INTEGRATION[0],
                                                     integration_version=INTEGRATION[1])
        return self._client

    def resolve(self, ref: str) -> str:
        async def go():
            return await (await self._get_client()).secrets.resolve(ref)

        return self._run(go)

    def items(self, vault_name: str) -> list[dict]:
        """Items of the vault with their fields' names and types (values dropped here)."""

        async def go():
            client = await self._get_client()
            vid = next((v.id for v in await client.vaults.list() if v.title == vault_name), None)
            if vid is None:
                raise Unavailable(f"the service account cannot see a vault called {vault_name!r}")
            out = []
            for ov in await client.items.list(vid):
                item = await client.items.get(vid, ov.id)
                sections = {s.id: s.title for s in (getattr(item, "sections", None) or [])}
                fields = [FieldMeta(id=f.id, title=f.title, type=_enum(getattr(f, "field_type", "")),
                                    section=sections.get(getattr(f, "section_id", None)) or None).__dict__
                          for f in (item.fields or [])]
                del item  # the full item holds values; only the metadata above leaves this function
                out.append({"id": ov.id, "title": ov.title, "category": _enum(getattr(ov, "category", "")),
                            "fields": fields})
            return out

        return self._run(go)


def _enum(v) -> str:
    return str(getattr(v, "value", v) or "")


_provider = None
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def provider():
    global _provider
    if _provider is None:
        _provider = SdkProvider()
    return _provider


def set_provider(p) -> None:
    """Tests: a fake with resolve(ref) and items(vault)."""
    global _provider
    _provider = p
    clear_cache()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def resolve(ref: str) -> str:
    """The value behind op://vault/item/field, from the short cache or 1Password."""
    on, why = configured()
    if not on:
        raise Unavailable(why)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(ref)
        if hit and hit[1] > now:
            return hit[0]
    value = provider().resolve(ref)
    if not isinstance(value, str) or value == "":
        raise Unavailable("1Password returned no value for this reference")
    ttl = cache_seconds()
    if ttl:
        with _cache_lock:
            _cache[ref] = (value, now + ttl)
    return value


def forget(ref: str) -> None:
    with _cache_lock:
        _cache.pop(ref, None)


def items() -> list[dict]:
    on, why = configured()
    if not on:
        raise Unavailable(why)
    return provider().items(vault())
