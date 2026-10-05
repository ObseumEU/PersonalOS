"""A cheap daily UX smoke of the web app, from inside the API container (no browser, no tokens).

It loads the main page and the mobile page (`/`, `/m`) from the web container, then every script,
stylesheet and module preload they reference, and records HTTP errors, failed requests and slow
loads. No login, no cookie, no device: the pages are the static app shell, so nothing is created
in the data. Results feed the daily digest as `ux:*` signals (pos.improve.signals).

What it does not do: console errors after the app's JavaScript runs. That needs a real browser
(Playwright), which neither the API nor the agent pool image carries for a scheduled job; the
browser the agents drive (Playwright MCP) logs in as an agent and would register a device. Too heavy
for a daily check; a failed bundle or a 5xx on the shell is what broke the app before.

Settings: `POS_UX_SMOKE=0` switches it off; `POS_UX_SMOKE_URL` (default `http://web`) is the base.
"""

import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

PAGES = ("/", "/m")
SLOW_PAGE_S = 1.5
SLOW_TOTAL_S = 5.0
MAX_ASSETS = 25
TIMEOUT_S = 10
_ASSET = re.compile(r"""<(?:script[^>]*\bsrc|link[^>]*\bhref)\s*=\s*["']([^"']+)["'][^>]*>""", re.I)
_LINK_REL = re.compile(r"""\brel\s*=\s*["']([^"']+)["']""", re.I)
_HASH = re.compile(r"[-.][A-Za-z0-9_]{8,}(?=\.[a-z0-9]+$)")


def base_url() -> str | None:
    if os.environ.get("POS_UX_SMOKE", "1").strip() == "0":
        return None
    return (os.environ.get("POS_UX_SMOKE_URL") or "http://web").rstrip("/")


def fetch(url: str) -> tuple[int, bytes, float]:
    """(status, body, seconds). A network failure is status 0."""
    t = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "PersonalOS-ux-smoke/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - our own web container
            body = r.read()
            return r.status, body, time.monotonic() - t
    except urllib.error.HTTPError as e:
        return e.code, b"", time.monotonic() - t
    except Exception:  # noqa: BLE001 - refused, timeout, DNS: a failed request
        return 0, b"", time.monotonic() - t


def reachable(base: str) -> bool:
    host = urlparse(base).hostname
    if not host:
        return False
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def stable_path(path: str) -> str:
    """An asset path without its build hash (`/assets/index-Bx81kq2a.js` -> `/assets/index.js`), so the
    signal key survives a rebuild."""
    p = urlparse(path).path or "/"
    return _HASH.sub("", p)


def assets(html: str, page_url: str) -> list[str]:
    out = []
    for m in _ASSET.finditer(html):
        tag = m.group(0)
        if tag.lower().startswith("<link"):
            rel = (_LINK_REL.search(tag) or [None, ""])[1].lower()
            if not any(k in rel for k in ("stylesheet", "modulepreload", "preload", "manifest")):
                continue
        url = urljoin(page_url, m.group(1))
        if urlparse(url).netloc == urlparse(page_url).netloc and url not in out:
            out.append(url)
    return out[:MAX_ASSETS]


def run(base: str | None = None) -> dict:
    """{"ran": bool, "pages": [...], "items": {key: signal item}} (items: pos.improve.signals' shape)."""
    base = base if base is not None else base_url()
    if not base:
        return {"ran": False, "skipped": "switched off (POS_UX_SMOKE=0)", "items": {}}
    if not reachable(base):
        return {"ran": False, "skipped": f"{urlparse(base).hostname} does not resolve", "items": {}}
    items: dict[str, dict] = {}

    def add(key: str, label: str, example: str, sample: str) -> None:
        it = items.setdefault(key, {"key": key, "category": "ux", "label": label, "count": 0.0, "examples": [],
                                    "sample": sample[:240]})
        it["count"] += 1
        if example not in it["examples"] and len(it["examples"]) < 5:
            it["examples"].append(example)

    pages, seen = [], set()
    for path in PAGES:
        url = base + path
        status, body, secs = fetch(url)
        total = secs
        page = {"path": path, "status": status, "seconds": round(secs, 3), "assets": 0, "failed": []}
        if status != 200:
            add(f"ux:http_error:{path}", f"page {path} did not load", path, f"HTTP {status or 'no answer'} for {path}")
            pages.append(page)
            continue
        for a in assets(body.decode("utf-8", "replace"), url):
            if a in seen:
                continue
            seen.add(a)
            st, _, s2 = fetch(a)
            total += s2
            page["assets"] += 1
            if st != 200:
                sp = stable_path(a)
                page["failed"].append(sp)
                add(f"ux:failed_request:{sp}", "a page asset failed to load", path, f"HTTP {st or 'no answer'} for {sp}")
        page["total_seconds"] = round(total, 3)
        if secs > SLOW_PAGE_S or total > SLOW_TOTAL_S:
            add(f"ux:slow:{path}", "slow page load", path, f"{path}: page {secs:.1f} s, with assets {total:.1f} s")
        pages.append(page)
    return {"ran": True, "pages": pages, "items": items}
