"""Redaction of secret values from anything that goes back to a model, a log or storage.

The same file lives in the worker (worker/pos_worker/redact.py); a test keeps
the two byte-identical, so the worker needs no import from the core.

For every secret it removes the plain value and its common encodings:
base64 (standard and URL-safe, with or without padding, also when the secret
sits inside a longer base64 blob such as HTTP basic auth, at any of the three
byte alignments), URL-encoding (quote and quote_plus) and hex. `Stream`
redacts output that arrives in chunks: a value split across two chunks is
still caught, because the tail that could still be part of a secret is held
back until the next chunk (or `close`), and the result is exactly what
redacting the whole output at once gives.
"""

import base64
import binascii
import re
from urllib.parse import quote, quote_plus

MIN_ENCODED = 8  # shorter encoded fragments would redact random text


def _b64_cores(raw: bytes) -> set[str]:
    """Base64 fragments that appear whenever `raw` is encoded inside a longer
    byte string, whatever its offset: for each alignment, the characters that
    depend only on the secret's bytes."""
    out = set()
    n = len(raw)
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        whole = enc(raw).decode()
        out.update({whole, whole.rstrip("=")})
        for k in range(3):
            full = enc(b"\0" * k + raw).decode()
            core = full[4 if k else 0:((k + n) // 3) * 4]
            if len(core) >= MIN_ENCODED:
                out.add(core)
    return out


def variants(secret: str) -> set[str]:
    """The strings to remove for one secret value."""
    if not secret:
        return set()
    out = {secret}
    raw = secret.encode()
    if len(secret) >= 4:
        out |= {quote(secret, safe=""), quote_plus(secret), quote(secret)}
        out |= {binascii.hexlify(raw).decode(), binascii.hexlify(raw).decode().upper()}
        out |= _b64_cores(raw)
    return {v for v in out if v}


class Redactor:
    """Replaces every known secret (and its encodings) with [REDACTED:<name>]."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self.marks: dict[str, str] = {}
        self._re: re.Pattern | None = None
        for name, value in (secrets or {}).items():
            self.add(name, value)

    def add(self, name: str, value: str) -> None:
        for v in variants(value or ""):
            self.marks.setdefault(v, f"[REDACTED:{name}]")
        # Longest first: at any position the longest encoding wins, never half of one.
        alts = sorted(self.marks, key=lambda v: (-len(v), v))
        self._re = re.compile("|".join(map(re.escape, alts))) if alts else None

    @property
    def longest(self) -> int:
        return max((len(v) for v in self.marks), default=0)

    def _sub(self, text: str, stop: int | None = None) -> tuple[str, int]:
        """Redact matches that start before `stop`; returns (text up to where it is final, that position)."""
        out, pos = [], 0
        for m in self._re.finditer(text):
            if stop is not None and m.start() >= stop:
                break
            out += [text[pos:m.start()], self.marks[m.group(0)]]
            pos = m.end()
        end = len(text) if stop is None else max(stop, pos)
        out.append(text[pos:end])
        return "".join(out), end

    def __call__(self, text: str) -> str:
        if not text or self._re is None:
            return text
        return self._sub(text)[0]

    def stream(self) -> "Stream":
        return Stream(self)


class Stream:
    """Chunked output: feed() returns what is safe to pass on now; close() the rest.
    A match that starts at least `longest - 1` characters before the end of what
    arrived is final; anything later waits for more output."""

    def __init__(self, redactor: Redactor):
        self.r = redactor
        self.held = ""

    def feed(self, chunk: str) -> str:
        buf = self.held + chunk
        if self.r._re is None:
            self.held = ""
            return buf
        stop = len(buf) - (self.r.longest - 1)
        if stop <= 0:
            self.held = buf
            return ""
        out, end = self.r._sub(buf, stop)
        self.held = buf[end:]
        return out

    def close(self) -> str:
        out, self.held = self.r(self.held), ""
        return out
