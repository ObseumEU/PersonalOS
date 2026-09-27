"""Redaction of secret values from anything that goes back to a model, a log or storage.

The same file lives in the worker (worker/pos_worker/redact.py); a test keeps
the two byte-identical, so the worker needs no import from the core.

For every secret it removes the plain value and its common encodings:
base64 (standard and URL-safe, with or without padding, also when the secret
sits inside a longer base64 blob such as HTTP basic auth, at any of the three
byte alignments), URL-encoding (quote and quote_plus, once or twice, upper or
lower case hex) and hex. On top of those literal strings, each secret gets one
pattern that accepts every character either as itself or escaped: JSON
(`\\uXXXX` in any case, surrogate pairs, `\\/`, `\\"`, `\\\\`, `\\n`...) or HTML
(`&#NN;`, `&#xNN;`, named entities such as `&quot;`), so a value echoed back
fully or only partly escaped is caught as well. Everything is precomputed when
a secret is added. `Stream`
redacts output that arrives in chunks: a value split across two chunks is
still caught, because the tail that could still be part of a secret is held
back until the next chunk (or `close`), and the result is exactly what
redacting the whole output at once gives.
"""

import base64
import binascii
import re
from html.entities import html5
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


def _url_forms(secret: str) -> set[str]:
    """URL-encoded once or twice, with upper or lower case hex at either level."""
    def lower(v: str) -> str:
        return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), v)

    once = {quote(secret, safe=""), quote_plus(secret), quote(secret)}
    once |= {lower(v) for v in once}
    twice = {quote(v, safe="") for v in once} | {quote_plus(v) for v in once}
    return once | twice | {lower(v) for v in twice}


def variants(secret: str) -> set[str]:
    """The literal strings to remove for one secret value (the escaped forms are in `pattern`)."""
    if not secret:
        return set()
    out = {secret}
    raw = secret.encode()
    if len(secret) >= 4:
        out |= _url_forms(secret)
        out |= {binascii.hexlify(raw).decode(), binascii.hexlify(raw).decode().upper()}
        out |= _b64_cores(raw)
    return {v for v in out if v}


_JSON_SHORT = {'"': r'\"', "\\": r"\\", "/": r"\/", "\b": r"\b", "\f": r"\f", "\n": r"\n", "\r": r"\r",
               "\t": r"\t"}
_NAMED: dict[str, list[str]] = {}
for _name, _ch in html5.items():
    if len(_ch) == 1:
        _NAMED.setdefault(_ch, []).append("&" + _name)
_ZEROS = 4  # leading zeros accepted in a numeric entity (bounded, so a match has a maximum length)


def _hex4(n: int) -> str:
    return r"\\u(?i:%04x)" % n


def _char(c: str) -> tuple[str, int]:
    """A regex for one character as itself or any of its JSON / HTML escapes, and its longest match."""
    n = ord(c)
    alts: list[tuple[str, int]] = [(re.escape(c), 1)]
    if n <= 0xFFFF:
        alts.append((_hex4(n), 6))
    else:
        hi, lo = 0xD800 + ((n - 0x10000) >> 10), 0xDC00 + ((n - 0x10000) & 0x3FF)
        alts.append((_hex4(hi) + _hex4(lo), 12))
    if c in _JSON_SHORT:
        alts.append((re.escape(_JSON_SHORT[c]), 2))
    dec, hx = str(n), "%x" % n
    alts.append(("&#0{0,%d}%s;?" % (_ZEROS, dec), 3 + _ZEROS + len(dec)))
    alts.append(("&#(?i:x)0{0,%d}(?i:%s);?" % (_ZEROS, hx), 4 + _ZEROS + len(hx)))
    alts += [(re.escape(e), len(e)) for e in _NAMED.get(c, ())]
    alts.sort(key=lambda a: -a[1])  # longest alternative first
    return "(?:" + "|".join(a for a, _ in alts) + ")", max(m for _, m in alts)


def pattern(secret: str) -> tuple[str, int]:
    """A regex matching `secret` with each character plain or JSON/HTML-escaped (all, some or none
    of them), and the longest text it can match."""
    parts = [_char(c) for c in secret]
    return "".join(p for p, _ in parts), sum(m for _, m in parts)


class Redactor:
    """Replaces every known secret (and its encodings) with [REDACTED:<name>]."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self.marks: dict[str, str] = {}  # literal encoding -> mark
        self.flex: dict[str, tuple[str, int, int, str]] = {}  # secret -> (regex, shortest, longest, mark)
        self._re: re.Pattern | None = None
        self._groups: dict[str, str] = {}
        for name, value in (secrets or {}).items():
            self.add(name, value)

    def add(self, name: str, value: str) -> None:
        mark = f"[REDACTED:{name}]"
        for v in variants(value or ""):
            self.marks.setdefault(v, mark)
        if value and value not in self.flex:
            src, most = pattern(value)
            self.flex[value] = (src, len(value), most, mark)
        # Longest first: at any position the longest encoding wins, never half of one. The escaped
        # forms of a secret are never shorter than it, so they rank by the secret's own length.
        alts = [(len(v), 1, re.escape(v), None) for v in self.marks]
        self._groups = {}
        for i, (src, least, _, m) in enumerate(self.flex.values()):
            self._groups[f"s{i}"] = m
            alts.append((least, 0, f"(?P<s{i}>{src})", m))
        alts.sort(key=lambda a: (-a[0], a[1], a[2]))
        self._re = re.compile("|".join(a[2] for a in alts)) if alts else None

    @property
    def longest(self) -> int:
        return max([len(v) for v in self.marks] + [f[2] for f in self.flex.values()], default=0)

    def _mark(self, m: re.Match) -> str:
        return self._groups[m.lastgroup] if m.lastgroup else self.marks[m.group(0)]

    def _sub(self, text: str, stop: int | None = None) -> tuple[str, int]:
        """Redact matches that start before `stop`; returns (text up to where it is final, that position)."""
        out, pos = [], 0
        for m in self._re.finditer(text):
            if stop is not None and m.start() >= stop:
                break
            out += [text[pos:m.start()], self._mark(m)]
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
