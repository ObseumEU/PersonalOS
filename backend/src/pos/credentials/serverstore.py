"""The second credential backend: values kept on the server itself, encrypted at rest.

Some secrets exist only on svr03 (the kniha-test passwords in /opt/server/kniha-test/.env, the copy of its data
key) and never reach 1Password: PersonalOS's service account is read-only on its vault (prod 2026-10-07, T-957).
They live here instead, encrypted like the LinkedIn app's secret (AES-GCM, a key derived with HKDF from
POS_SECRETS_KEY or the session secret), one file per entry: <data>/secrets/credentials/<name>.bin, mode 600.

An entry has named fields (`password`, `username`, `authorization` = base64 "user:password" for HTTP basic
auth). A registry entry points to one field with the reference `pos://<entry>/<field>` instead of op://…, and
from there on it is used exactly like a 1Password credential: the same grants, hosts, audit, use limits and
redaction (pos.credentials.service resolves either kind).

Only the operator or the platform writes entries, with the CLI inside the api container
(`python -m pos.credentials put …`, see __main__.py); no agent tool and no HTTP endpoint writes or reads one.
Values are never logged, listed or returned by anything but `resolve`, which only the credential service calls.
"""

import base64
import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .onepassword import Unavailable as _OpUnavailable

SCHEME = "pos://"
LABEL = "úložiště na serveru"
_MAGIC = b"PCS1"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,62}$")
FIELD_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
MAX_VALUE = 8000


class Unavailable(_OpUnavailable):
    """The entry, its field or the key is missing: fail closed (a subclass, so callers catch both backends)."""


def _settings():
    from ..config import get_settings

    return get_settings()


def _master() -> bytes:
    return (os.environ.get("POS_SECRETS_KEY") or _settings().session_secret or "").encode()


def configured() -> tuple[bool, str]:
    if not _master():
        return False, "neither POS_SECRETS_KEY nor the session secret is set on the server"
    return True, ""


def directory() -> Path:
    return Path(_settings().data_dir) / "secrets" / "credentials"


def _key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"pos-credentials-v1", info=b"server-store").derive(_master())


def is_ref(ref: str | None) -> bool:
    return str(ref or "").startswith(SCHEME)


def ref(name: str, field: str) -> str:
    return f"{SCHEME}{name}/{field}"


def parse_ref(value: str) -> tuple[str, str]:
    """pos://<entry>/<field> -> (entry, field); ValueError when it is not one."""
    body = str(value or "")
    if not body.startswith(SCHEME):
        raise ValueError("not a pos:// reference")
    name, _, field = body[len(SCHEME):].partition("/")
    if not NAME_RE.match(name) or not FIELD_RE.match(field):
        raise ValueError("a server-store reference is pos://<entry>/<field> (lower-case, like "
                         "pos://kniha-test-admin/authorization)")
    return name, field


def _path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"entry name {name!r}: 2-63 characters, a-z 0-9 . _ -")
    return directory() / f"{name}.bin"


def _read(name: str) -> dict | None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = _path(name).read_bytes()
    except (OSError, ValueError):
        return None
    if not blob.startswith(_MAGIC):
        return None
    try:
        return json.loads(AESGCM(_key()).decrypt(blob[4:16], blob[16:], name.encode()))
    except (InvalidTag, ValueError):
        return None


def put(name: str, fields: dict[str, str], *, source: str = "", replace: bool = False) -> dict:
    """Store (or update) an entry's fields. Returns its metadata (field names, never values). Operator only:
    called by the CLI, never by an agent tool or an HTTP endpoint."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    on, why = configured()
    if not on:
        raise Unavailable(why)
    clean: dict[str, str] = {}
    for k, v in (fields or {}).items():
        k = str(k).strip().lower()
        if not FIELD_RE.match(k):
            raise ValueError(f"field {k!r}: 1-40 characters, a-z 0-9 . _ -")
        v = str(v)
        if not v or len(v) > MAX_VALUE or "\n" in v or "\r" in v:
            raise ValueError(f"field {k}: a non-empty one-line value (at most {MAX_VALUE} characters)")
        clean[k] = v
    if not clean:
        raise ValueError("no field to store")
    old = None if replace else _read(name)
    data = {"fields": {**((old or {}).get("fields") or {}), **clean},
            "source": str(source or (old or {}).get("source") or "")[:300],
            "created_at": (old or {}).get("created_at") or _now(), "updated_at": _now()}
    nonce = os.urandom(12)
    blob = _MAGIC + nonce + AESGCM(_key()).encrypt(nonce, json.dumps(data).encode(), name.encode())
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(p)
    return meta(name) or {}


def basic_fields(user: str, password: str) -> dict[str, str]:
    """username, password and authorization (base64 "user:password" for an 'Authorization: Basic' header)."""
    return {"username": user, "password": password,
            "authorization": base64.b64encode(f"{user}:{password}".encode()).decode()}


def delete(name: str) -> bool:
    try:
        _path(name).unlink()
        return True
    except (OSError, ValueError):
        return False


def meta(name: str) -> dict | None:
    """An entry's field names and times (never a value); None when there is none (or it cannot be read)."""
    d = _read(name)
    if d is None:
        return None
    return {"name": name, "fields": sorted((d.get("fields") or {}).keys()), "source": d.get("source") or "",
            "created_at": d.get("created_at"), "updated_at": d.get("updated_at")}


def names() -> list[str]:
    try:
        return sorted(p.stem for p in directory().glob("*.bin") if NAME_RE.match(p.stem))
    except OSError:
        return []


def entries() -> list[dict]:
    return [m for m in (meta(n) for n in names()) if m]


def resolve(value_ref: str) -> str:
    """The value behind pos://entry/field. Raises Unavailable (its message says 'no such item' / 'no field' when
    the entry or field does not exist: the grounding check reads that as a definite 'missing')."""
    on, why = configured()
    if not on:
        raise Unavailable(why)
    try:
        name, field = parse_ref(value_ref)
    except ValueError as e:
        raise Unavailable(f"invalid secret reference: {e}") from None
    d = _read(name)
    if d is None:
        if _path(name).exists():
            raise Unavailable(f"the server-store entry {name} cannot be decrypted (was the key changed?)")
        raise Unavailable(f"no such item in the server store: {name}")
    value = (d.get("fields") or {}).get(field)
    if not value:
        raise Unavailable(f"no field {field!r} in the server-store entry {name}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
