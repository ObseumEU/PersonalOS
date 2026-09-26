"""Log lines → (is it an error?, its fingerprint).

A fingerprint is the hash of the line with everything that varies between
two occurrences of "the same" error replaced by a placeholder: timestamps,
UUIDs, hex ids, e-mail addresses, IPs, URLs' query strings, paths, quoted
values and numbers. 10,000 identical errors become one fingerprint with a
count.
"""

import hashlib
import json
import re

# Order matters: the specific shapes first, bare numbers last.
_SUBS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b(?:sk|pk|pos|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}"), "<key>"),
    (re.compile(r"\bBearer\s+\S+", re.I), "Bearer <key>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"https?://[^\s\"'<>]+"), "<url>"),
    (re.compile(r"(?<![\w.])(?:/[\w@.+-]+){2,}/?"), "<path>"),
    (re.compile(r"\b(?:0x)?[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9_-]{24,}\b"), "<id>"),
    (re.compile(r"'[^']{0,200}'"), "'<v>'"),
    (re.compile(r'"[^"]{0,200}"'), '"<v>"'),
    (re.compile(r"(?<![A-Za-z])-?\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]

_ERROR_WORDS = re.compile(r"\b(ERROR|CRITICAL|FATAL|PANIC|Traceback \(most recent call last\)|Unhandled|"
                          r"[A-Z][A-Za-z]+(?:Error|Exception)\b)")
_NOT_ERROR = re.compile(r"\b(?:0 errors?|no errors?|errors?=0|error_count\W+0)\b", re.I)
# uvicorn / nginx / caddy style access lines: "GET /x HTTP/1.1" 500
_ACCESS = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (\S+) HTTP/[\d.]+"\s+(\d{3})')
_ROUTE_ID = re.compile(r"/(?:\d+|[0-9a-f-]{16,}|[A-Za-z0-9_.@-]{24,})(?=/|$)", re.I)
_LEVELS_ERR = {"error", "err", "critical", "crit", "fatal", "panic", "alert", "emergency"}


def normalize(text: str) -> str:
    out = text.strip()
    for rx, rep in _SUBS:
        out = rx.sub(rep, out)
    return out[:400]


def fingerprint(service: str, text: str) -> str:
    return hashlib.sha1(f"{service}|{normalize(text)}".encode("utf-8", "replace")).hexdigest()[:16]


def key_of(cls: dict) -> str:
    """The normalized text a classified line is counted under."""
    return cls.get("key") or normalize(cls["text"])


def fp_of(service: str, cls: dict) -> str:
    return hashlib.sha1(f"{service}|{key_of(cls)}".encode("utf-8", "replace")).hexdigest()[:16]


def _access(m: re.Match, text: str) -> dict:
    status = int(m.group(3))
    route = _ROUTE_ID.sub("/<id>", m.group(2).split("?", 1)[0])[:120]
    return {"error": status >= 500, "status": status, "path": route, "text": text,
            "key": f"HTTP {status} {m.group(1)} {route}"}


def _json(line: str) -> dict | None:
    s = line.lstrip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def classify(line: str) -> dict:
    """What the line says: {"error": bool, "status": int|None, "path": str|None,
    "text": the message to fingerprint}. JSON logs (pino, structlog, LiteLLM)
    are read by their level and status fields; plain lines by level words and
    access-log status codes."""
    d = _json(line)
    if d is not None:
        msg = str(d.get("msg") or d.get("message") or d.get("event") or d.get("error") or "")
        err = d.get("err") or d.get("error")
        if isinstance(err, dict):
            msg = f"{msg} {err.get('type', '')}: {err.get('message', '')}".strip()
        level = d.get("level", d.get("severity", d.get("levelname")))
        status = None
        res = d.get("res")
        if isinstance(res, dict) and isinstance(res.get("statusCode"), int):
            status = res["statusCode"]
        elif isinstance(d.get("status_code"), int):
            status = d["status_code"]
        is_err = (isinstance(level, (int, float)) and level >= 50) or (
            isinstance(level, str) and level.lower() in _LEVELS_ERR)
        m = _ACCESS.search(msg)
        if m:
            return _access(m, msg)
        if status is not None and status >= 500:
            is_err = True
        req = d.get("req") if isinstance(d.get("req"), dict) else {}
        path = _ROUTE_ID.sub("/<id>", str(req.get("url") or "").split("?", 1)[0])[:120] or None
        return {"error": bool(is_err), "status": status, "path": path, "text": msg or line}
    m = _ACCESS.search(line)
    if m:
        return _access(m, line)
    is_err = bool(_ERROR_WORDS.search(line)) and not _NOT_ERROR.search(line)
    return {"error": is_err, "status": None, "path": None, "text": line}


QUOTA = re.compile(r"usage limit|rate[ _-]?limit(?:ed| exceeded| reached)|insufficient_quota|quota exceeded|"
                   r"budget (?:has been )?exceeded|exceeded (?:your|the) (?:current )?quota|max_budget", re.I)

# When an exhausted quota comes back, as the line names it:
#   "(skipping Codex until 2026-09-29T06:48:00.000Z)", "usage limit until 2026-09-26T16:11:42+00:00"
#   "try again at Sep 29th, 2026 6:47 AM" (no zone: the container's, UTC on svr03)
#   "You've hit your session limit · resets 11:20am (UTC)", "usage limit reached|1759161600"
_UNTIL_ISO = re.compile(r"\buntil (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)")
_AGAIN_AT = re.compile(r"try again at ([A-Z][a-z]{2})[a-z]* (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}),? (\d{1,2}):(\d{2}) ?([AP]M)",
                       re.I)
_RESETS = re.compile(r"resets (\d{1,2})(?::(\d{2}))? ?([ap]m)? ?\(UTC\)", re.I)
_EPOCH = re.compile(r"limit reached\|(\d{10})\b", re.I)
MAX_RESET_S = 14 * 86400


def quota_reset(line: str, now: float) -> float | None:
    """The earliest future reset time a quota line names (epoch seconds), or None."""
    from datetime import datetime, timedelta, timezone

    found: list[float] = []
    for m in _UNTIL_ISO.finditer(line):
        raw = m.group(1).replace("Z", "+00:00")
        try:
            t = datetime.fromisoformat(raw)
        except ValueError:
            continue
        found.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())
    for m in _AGAIN_AT.finditer(line):
        mon, day, year, hh, mm, ampm = m.groups()
        try:
            t = datetime.strptime(f"{mon[:3].title()} {day} {year} {hh}:{mm} {ampm.upper()}", "%b %d %Y %I:%M %p")
        except ValueError:
            continue
        found.append(t.replace(tzinfo=timezone.utc).timestamp())
    for m in _RESETS.finditer(line):
        h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm:
            h = h % 12 + (12 if ampm == "pm" else 0)
        if h > 23 or mi > 59:
            continue
        base = datetime.fromtimestamp(now, timezone.utc).replace(hour=h, minute=mi, second=0, microsecond=0)
        if base.timestamp() <= now:
            base += timedelta(days=1)
        found.append(base.timestamp())
    for m in _EPOCH.finditer(line):
        found.append(float(m.group(1)))
    future = [t for t in found if now < t <= now + MAX_RESET_S]
    return min(future) if future else None
