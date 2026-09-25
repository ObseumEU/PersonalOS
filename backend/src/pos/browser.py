"""Browser use for agents: the policy, the audit trail and the screenshots.

Agents drive a real browser through Playwright MCP, behind the worker's guard
proxy (worker/pos_worker/browser_guard.py). The guard asks `check` before every
tool call and reports each one to `record`.

The owner's rule (2026-09-25): browsing, reading, logged-in pages and filling in
forms need no approval. Approval is needed only for what the constitution calls
irreversible or outward: paying or buying, sending a message or e-mail on the
owner's behalf, deleting, and changing account or security settings. Banking
and payment sites always ask first. Everything is in the audit log, with a
screenshot of each action; page content reaches the agent as untrusted data.
Typed text is never stored (it may be a password).
"""

import base64
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import approvals, audit
from .core import Ctx

# Sites where any visit asks the owner first: banks and payment services.
APPROVAL_DOMAINS = (
    "csob.cz", "kb.cz", "csas.cz", "servis24.cz", "georgeapp.cz", "fio.cz", "rb.cz", "moneta.cz", "airbank.cz",
    "mbank.cz", "unicreditbank.cz", "creditas.cz", "equabank.cz", "trinitybank.cz", "revolut.com", "wise.com",
    "paypal.com", "stripe.com", "gopay.com", "comgate.cz", "thepay.cz", "coinbase.com", "binance.com",
)
RISKY_ACTION = re.compile(
    r"\b(buy|purchase|order now|place order|checkout|check out|pay|payment|pay now|subscribe|donate|transfer|"
    r"send|send now|delete|remove|erase|close account|deactivate|change password|reset password|two-factor|2fa|"
    r"security settings|zaplatit|platba|zaplaťte|koupit|objednat|odeslat|poslat|převést|převod|smazat|odstranit|"
    r"zrušit účet|změnit heslo|zabezpečení)\b",
    re.IGNORECASE,
)
MESSAGE_FIELD = re.compile(
    r"message|compose|reply|comment|chat|post|tweet|e-?mail body|zpráv|komentář|odpověď|napište|napiš", re.IGNORECASE)
RISKY_SCRIPT = re.compile(r"fetch\(|XMLHttpRequest|sendBeacon|\.submit\(|\.click\(|window\.open\(|navigator\.", re.I)
ACTION_TOOLS = {"browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
                "browser_navigate", "browser_evaluate", "browser_file_upload", "browser_drag", "browser_handle_dialog"}


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def approval_domains() -> tuple[str, ...]:
    extra = tuple(d.strip().lower() for d in os.environ.get("POS_BROWSER_APPROVAL_DOMAINS", "").split(",") if d.strip())
    return APPROVAL_DOMAINS + extra


def decide(tool: str, args: dict, *, url: str | None = None, last_field: str | None = None,
           allow_hosts: list[str] | None = None) -> tuple[str, str]:
    """("allow" | "approval", reason) for one browser tool call.
    `url` is the page the agent is on; `last_field` the element it typed into last."""
    target = args.get("url") if tool == "browser_navigate" else url
    host = _host(target)
    if host and _matches(host, approval_domains()):
        return "approval", f"{host} is a banking or payment site"
    if tool == "browser_navigate" and allow_hosts and host and not _matches(host, allow_hosts):
        return "approval", f"{host} is outside this agent's usual sites"
    element = str(args.get("element") or args.get("name") or "")
    if tool in ("browser_click", "browser_select_option") and RISKY_ACTION.search(element):
        return "approval", f"'{element[:80]}' looks like paying, sending, deleting or changing account settings"
    if tool == "browser_type" and args.get("submit") and (MESSAGE_FIELD.search(element) or RISKY_ACTION.search(element)):
        return "approval", f"submitting '{element[:80]}' would send something on the owner's behalf"
    if tool == "browser_press_key" and str(args.get("key", "")).lower() == "enter" and last_field \
            and MESSAGE_FIELD.search(last_field):
        return "approval", f"Enter in '{last_field[:80]}' would send a message"
    if tool == "browser_fill_form":
        names = " ".join(str(f.get("name", "")) for f in args.get("fields") or [])
        if re.search(r"card number|cvv|cvc|číslo karty|iban", names, re.IGNORECASE):
            return "approval", "the form asks for payment details"
    if tool == "browser_evaluate" and RISKY_SCRIPT.search(str(args.get("function") or "")):
        return "approval", "the script sends requests or clicks by itself"
    return "allow", "ok"


def redact(tool: str, args: dict) -> dict:
    """Arguments safe to store: typed text and form values are never kept."""
    out = {}
    for k, v in (args or {}).items():
        if k in ("text", "value", "values"):
            out[k] = f"<{len(str(v))} chars>"
        elif k == "fields" and isinstance(v, list):
            out[k] = [{**{kk: vv for kk, vv in f.items() if kk not in ("value",)}, "value": "<hidden>"}
                      for f in v if isinstance(f, dict)]
        else:
            out[k] = v if len(str(v)) < 500 else str(v)[:500] + "…"
    return out


def shots_dir(data_dir: Path) -> Path:
    return data_dir / "files" / "browser"


def save_screenshot(data_dir: Path, actor_id: int, tool: str, png_b64: str | None) -> str | None:
    if not png_b64:
        return None
    try:
        raw = base64.b64decode(png_b64, validate=True)
    except ValueError:
        return None
    if len(raw) > 8 * 1024 * 1024:
        return None
    now = datetime.now(timezone.utc)
    rel = Path(str(actor_id)) / now.strftime("%Y%m%d") / f"{now.strftime('%H%M%S%f')}-{re.sub(r'[^a-z_]', '', tool)}.png"
    path = shots_dir(data_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return rel.as_posix()


def check(conn: sqlite3.Connection, ctx: Ctx, data_dir: Path, body: dict) -> dict:
    """The guard's question before a tool call. With `dry_run` it only answers;
    otherwise an approval is requested, with the screenshot the guard took."""
    tool = str(body.get("tool") or "")
    args = body.get("args") or {}
    decision, reason = decide(tool, args, url=body.get("url"), last_field=body.get("last_field"),
                              allow_hosts=body.get("allow_hosts") or None)
    out = {"decision": decision, "reason": reason}
    if decision == "approval" and not body.get("dry_run"):
        shot = save_screenshot(data_dir, ctx.actor_id, tool, body.get("screenshot"))
        a = approvals.request(conn, ctx, f"browser: {tool.removeprefix('browser_')}", {
            "why": reason, "url": body.get("url"), "tool": tool, "args": redact(tool, args), "screenshot": shot,
        }, task_id=body.get("task_id"))
        out["approval_id"] = a["id"]
    conn.commit()
    return out


def record(conn: sqlite3.Connection, ctx: Ctx, data_dir: Path, body: dict) -> dict:
    tool = str(body.get("tool") or "")
    shot = save_screenshot(data_dir, ctx.actor_id, tool, body.get("screenshot"))
    audit.log(conn, ctx, f"browser:{tool.removeprefix('browser_')}", "task" if body.get("task_id") else None,
              body.get("task_id"), url=body.get("url"), args=redact(tool, body.get("args") or {}),
              ok=bool(body.get("ok", True)), screenshot=shot, approval_id=body.get("approval_id"))
    conn.commit()
    return {"screenshot": shot}


def screenshot_path(data_dir: Path, rel: str) -> Path | None:
    base = shots_dir(data_dir).resolve()
    p = (base / rel).resolve()
    return p if p.is_file() and base in p.parents and p.suffix == ".png" else None
