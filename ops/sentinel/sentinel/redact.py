"""Secrets and personal data out of anything that leaves the sentinel.

Log lines may hold API keys, bearer tokens, passwords in URLs, cookies and
e-mail addresses (knowlage and the Mail agent handle e-mail). Every sample
line, log excerpt and packet goes through `redact` first.
"""

import re

_RULES = [
    # key=value / "key": "value" pairs whose name says secret
    (re.compile(r"""(?i)(["']?(?:api[_-]?key|apikey|token|access[_-]?token|refresh[_-]?token|secret|password|passwd|"""
                r"""pwd|authorization|cookie|set-cookie|session|client[_-]?secret|private[_-]?key|master[_-]?key)"""
                r"""["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}]+)"""), r"\1<redacted>"),
    (re.compile(r"(?i)\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 <redacted>"),
    (re.compile(r"\b(?:sk|pk|rk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{8,}"), "<key>"),
    (re.compile(r"\b(?:pos|ghp|gho|ghs|ghu|glpat|xox[abpr])[-_][A-Za-z0-9_-]{8,}"), "<key>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"), "<jwt>"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s:/@]+:[^\s@/]+@"), r"\1<user>:<redacted>@"),
    (re.compile(r"(?i)([?&](?:key|token|sig|signature|code|secret|password|access_token)=)[^&\s\"']+"), r"\1<redacted>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    # long opaque strings that look like credentials (hex or base64, 32+ chars)
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "<blob>"),
]


def redact(text: str, limit: int = 300) -> str:
    out = text or ""
    for rx, rep in _RULES:
        out = rx.sub(rep, out)
    out = out.replace("\r", " ").replace("\x00", "")
    if len(out) > limit:
        out = out[: limit - 1] + "…"
    return out
