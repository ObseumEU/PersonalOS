"""A temporary 1Password write token, taken off the admin console without anyone seeing it.

1Password does not let an existing service account gain a vault or a permission ("After you create a service
account, you can't add additional vaults or edit any vault permissions it has"), so PersonalOS's read-only service
account can never be switched to write. When the platform must put items into its vault (prod 2026-10-07: the
kniha-test logins and the recovery copy of its data key, T-918), an agent prepares a **new, temporary** service
account with write access in the owner's 1Password admin console, the owner only logs in and clicks Create
(browser_request_owner_handoff), and the agent takes the token shown once off the page with
`browser_capture_secret(target="onepassword.write_token")`: the guard sends it here, the model never sees it.

The token is stored encrypted (like the LinkedIn app's secret), used by the operator's follow-up on the server
(`token()` inside the API container: it never leaves the process), then `discard()`ed; the owner deletes the
temporary service account in the same console (a second handoff the agent prepares).

Only an agent with an open task whose `source` is TASK_SOURCE may store it (the platform creates that task).
"""

import contextlib
import json
import os
import sqlite3
from pathlib import Path

TARGET = "onepassword.write_token"
TASK_SOURCE = "op-write-token"
# The 1Password web app: my.1password.com / <team>.1password.com, the EU and Canada regions.
HOST_SUFFIXES = ("1password.com", "1password.eu", "1password.ca")
_AAD = b"op-write-token"


def host_ok(host: str) -> bool:
    h = (host or "").lower()
    return any(h == s or h.endswith("." + s) for s in HOST_SUFFIXES)


def _settings():
    from .config import get_settings

    return get_settings()


def _path() -> Path:
    return Path(_settings().data_dir) / "secrets" / "op-write-token.bin"


def _key() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    master = (os.environ.get("POS_SECRETS_KEY") or _settings().session_secret or "").encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"pos-op-write-v1", info=b"owner").derive(master)


def open_task(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE source = ? AND assignee_id = ? AND archived_at IS NULL AND "
                        "status NOT IN ('done', 'cancelled') ORDER BY id DESC LIMIT 1",
                        (TASK_SOURCE, agent_id)).fetchone()


def capture(conn: sqlite3.Connection, agent_id: int, value: str) -> int:
    """The token the agent's guard took off the page. Returns its length; never logs or returns the value."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if open_task(conn, agent_id) is None:
        raise PermissionError("no open 1Password write-token task is yours (the platform creates it)")
    value = (value or "").strip()
    if not value.startswith("ops_") or len(value) > 4000 or len(value) < 40:
        raise ValueError("that is not a 1Password service account token (ops_…): take the token field itself, "
                         "revealed (reveal_ref) if masked")
    nonce = os.urandom(12)
    blob = b"POW1" + nonce + AESGCM(_key()).encrypt(nonce, json.dumps({"token": value}).encode(), _AAD)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(blob)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(p)
    return len(value)


def token() -> str | None:
    """The stored token, for the operator's follow-up inside this container only."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = _path().read_bytes()
    except OSError:
        return None
    if not blob.startswith(b"POW1"):
        return None
    try:
        return json.loads(AESGCM(_key()).decrypt(blob[4:16], blob[16:], _AAD))["token"]
    except (InvalidTag, ValueError, KeyError):
        return None


def present() -> bool:
    return _path().exists()


def discard() -> None:
    with contextlib.suppress(OSError):
        _path().unlink()
