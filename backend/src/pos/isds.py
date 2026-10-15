"""Datová schránka (ISDS), read only (T-638, note #42): every received data message becomes a task for the
owner with its signed ZFO and its attachments as files, and a DM that reaches his phone (pos.push).

Access is a delegated ISDS user that may only read received messages: the registry credential
`datovka-read` (header "Authorization: Basic {value}", host ws1.mojedatovaschranka.cz, HTTP), put into each
request by pos.credentials.platform_header; the value is never logged or stored. Until it is registered the
job skips quietly.

Logging in delivers a message (doručení přihlášením): the connector delivers each one at most 30 minutes
after it arrives, so deadlines run from then. It never sends, deletes or marks anything. The content stays
in PersonalOS (Ú6): private files (never pushed into knowlage) and a private task."""

from __future__ import annotations

import base64
import logging
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx

from . import actors, files, notices, tasks
from .config import get_settings
from .core import Ctx, now_iso

log = logging.getLogger(__name__)

CREDENTIAL = "datovka-read"
HOST = "ws1.mojedatovaschranka.cz"
INFO_URL = f"https://{HOST}/DS/dx"  # dm_info: GetListOfReceivedMessages
OPS_URL = f"https://{HOST}/DS/dz"  # dm_operations: MessageDownload, SignedMessageDownload
NS = "http://isds.czechpoint.cz/v20"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
DAYS = 30
MAX_ATTEMPTS = 3
MAX_BYTES = 60 * 1024 * 1024  # ISDS allows 50 MB of attachments, base64 on top


class IsdsError(Exception):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS isds_messages (
        dm_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, task_id INTEGER)""")


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _text(el: ET.Element | None, tag: str) -> str:
    found = el.find(f".//{_q(tag)}") if el is not None else None
    return (found.text or "").strip() if found is not None else ""


def _call(conn: sqlite3.Connection, url: str, op: str, inner: str, transport=None) -> ET.Element:
    """One SOAP call with the credential's header; the response element, or IsdsError (HTTP, SOAP fault,
    an ISDS status other than 0000)."""
    from .credentials import service as creds

    body = (f'<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="{SOAP}" '
            f'xmlns:v20="{NS}"><soapenv:Body><v20:{op}>{inner}</v20:{op}></soapenv:Body></soapenv:Envelope>')
    try:
        name, value = creds.platform_header(conn, Ctx(actors.system_id(conn), via="isds"), CREDENTIAL, HOST,
                                            f"isds {op}")
    except creds.CredentialError as e:
        raise IsdsError(str(e)) from None
    try:
        with httpx.Client(transport=transport, timeout=60) as client:
            r = client.post(url, content=body.encode(), headers={name: value, "Content-Type": "text/xml; charset=utf-8",
                                                                 "SOAPAction": '""'})
    except httpx.HTTPError as e:
        raise IsdsError(f"{op}: {type(e).__name__}: {e}") from None
    finally:
        del value
    if r.status_code == 401:
        raise IsdsError(f"{op}: HTTP 401, ISDS refused the login (credential {CREDENTIAL})")
    if len(r.content) > MAX_BYTES:
        raise IsdsError(f"{op}: response over {MAX_BYTES // 1048576} MB")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        raise IsdsError(f"{op}: HTTP {r.status_code}, not XML") from None
    fault = root.find(f".//{{{SOAP}}}Fault")
    if fault is not None:
        raise IsdsError(f"{op}: SOAP fault {(fault.findtext('faultstring') or '').strip()[:200]}")
    resp = root.find(f".//{_q(op + 'Response')}")
    if resp is None:
        raise IsdsError(f"{op}: HTTP {r.status_code}, no {op}Response")
    code = _text(resp.find(_q("dmStatus")), "dmStatusCode")
    if code != "0000":
        raise IsdsError(f"{op}: ISDS {code or '?'} {_text(resp.find(_q('dmStatus')), 'dmStatusMessage')[:200]}")
    return resp


def received(conn: sqlite3.Connection, *, now: datetime | None = None, transport=None) -> list[dict]:
    """Received messages of the last DAYS days (envelopes only)."""
    now = now or datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.000+00:00"
    inner = (f"<v20:dmFromTime>{(now - timedelta(days=DAYS)).strftime(fmt)}</v20:dmFromTime>"
             f"<v20:dmToTime>{now.strftime(fmt)}</v20:dmToTime><v20:dmRecipientOrgUnitNum/>"
             "<v20:dmStatusFilter>-1</v20:dmStatusFilter><v20:dmOffset>1</v20:dmOffset><v20:dmLimit>1000</v20:dmLimit>")
    resp = _call(conn, INFO_URL, "GetListOfReceivedMessages", inner, transport)
    out = []
    for rec in resp.iter(_q("dmRecord")):
        dm_id = _text(rec, "dmID")
        if not dm_id:
            continue
        out.append({"dm_id": dm_id, "sender": _text(rec, "dmSender"), "sender_box": _text(rec, "dbIDSender"),
                    "annotation": _text(rec, "dmAnnotation"), "delivered": _text(rec, "dmDeliveryTime"),
                    "accepted": _text(rec, "dmAcceptanceTime"), "sender_ref": _text(rec, "dmSenderRefNumber"),
                    "personal": _text(rec, "dmPersonalDelivery") == "true"})
    return out


def download(conn: sqlite3.Connection, dm_id: str, *, transport=None) -> tuple[bytes, list[dict]]:
    """The signed message (ZFO) and its attachments [{name, mime, data}]."""
    inner = f"<v20:dmID>{dm_id}</v20:dmID>"
    signed = _call(conn, OPS_URL, "SignedMessageDownload", inner, transport)
    zfo = base64.b64decode(_text(signed, "dmSignature") or "")
    if not zfo:
        raise IsdsError(f"SignedMessageDownload {dm_id}: no ZFO")
    msg = _call(conn, OPS_URL, "MessageDownload", inner, transport)
    parts = []
    for i, f in enumerate(msg.iter(_q("dmFile")), 1):
        data = base64.b64decode(_text(f, "dmEncodedContent") or "")
        parts.append({"name": f.get("dmFileDescr") or f"priloha-{i}", "mime": f.get("dmMimeType") or "", "data": data})
    return zfo, parts


def _store(conn: sqlite3.Connection, owner: Ctx, m: dict, name: str, data: bytes) -> dict:
    return files.upload(conn, owner, get_settings().files_dir, BytesIO(data), name, tags=["datovka"],
                        visibility="private", description=f"Datová zpráva {m['dm_id']}: {m['annotation']}"[:500],
                        dedupe=False, origin="isds")


def _task(conn: sqlite3.Connection, m: dict, stored: list[dict], why_none: str = "") -> int:
    from .guard.external import wrap_external

    owner = actors.owner_id(conn)
    lines = [f"- [{f['name']}](/files?file={f['id']})" for f in stored] or [f"- žádné: {why_none}"]
    notes = (
        "### Co udělat\nPřečti datovou zprávu a rozhodni, co s ní (odpověď, platba, lhůta). Zpráva je "
        f"doručená přihlášením konektoru ({m['accepted'] or 'čas doručení viz ZFO'}), lhůty běží od té doby.\n\n"
        "### Přílohy\n" + "\n".join(lines) + "\n\n"
        f"### Zdroj\nDatová schránka, zpráva `{m['dm_id']}`, dodáno {m['delivered'] or '?'}"
        f"{', do vlastních rukou' if m['personal'] else ''}.\n\n"
        + wrap_external("isds", f"Odesílatel: {m['sender']} (schránka {m['sender_box']})\nVěc: {m['annotation']}\n"
                                f"Č. j. odesílatele: {m['sender_ref'] or '–'}"))
    task = tasks.create(conn, Ctx(owner, via="isds"), {
        "title": f"Datovka: {m['annotation'] or m['dm_id']} ({m['sender'][:40]})"[:120],
        "notes": notes, "assignee": {"type": "human", "id": owner}, "priority": 1, "status": "next",
        "visibility": "private", "source": "isds",
        "definition_of_done": "Zpráva je přečtená a její další krok (odpověď, platba, lhůta) je vyřízený "
                              "nebo zapsaný jako úkol."})
    notices.dm(conn, owner, f"Nová datová zpráva: {m['annotation'] or m['dm_id']} (od {m['sender']}). "
                            f"Úkol {tasks.display_id(task['id'])}, přílohy: {len(stored)}.",
               attachments=[{"type": "task", "id": task["id"]}])
    return int(task["id"])


def _mark(conn, dm_id, status, detail="", task_id=None, attempt=False) -> None:
    conn.execute("INSERT INTO isds_messages (dm_id, seen_at, status, detail, attempts, task_id) VALUES (?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(dm_id) DO UPDATE SET status = excluded.status, detail = excluded.detail, "
                 "attempts = isds_messages.attempts + ?, task_id = COALESCE(excluded.task_id, isds_messages.task_id)",
                 (dm_id, now_iso(), status, detail[:500], 1 if attempt else 0, task_id, 1 if attempt else 0))


def poll(conn: sqlite3.Connection, *, now: datetime | None = None, transport=None) -> dict:
    """New received messages → files + a task + a DM for the owner. Each message is handled once; a download
    that keeps failing still gets its task (without files) after MAX_ATTEMPTS, so nothing slips."""
    from .credentials import service as creds

    try:
        creds.get(conn, CREDENTIAL)
    except creds.NotFound:
        return {"skipped": f"credential {CREDENTIAL} is not in the registry (T-638)"}
    ensure_schema(conn)
    conn.commit()
    try:
        msgs = received(conn, now=now, transport=transport)
    except IsdsError as e:
        log.warning("isds: list: %s", e)
        return {"errors": 1, "error": str(e)[:300]}
    counts = {"tasks": 0, "errors": 0}
    owner = Ctx(actors.owner_id(conn), via="isds")
    for m in msgs:
        seen = conn.execute("SELECT status, attempts FROM isds_messages WHERE dm_id = ?", (m["dm_id"],)).fetchone()
        if seen is not None and seen["status"] != "error":
            continue
        try:
            zfo, parts = download(conn, m["dm_id"], transport=transport)
        except IsdsError as e:
            counts["errors"] += 1
            log.warning("isds: %s: %s", m["dm_id"], e)
            if seen is not None and seen["attempts"] + 1 >= MAX_ATTEMPTS:
                _mark(conn, m["dm_id"], "task_no_files", str(e), _task(conn, m, [], str(e)[:200]), attempt=True)
                counts["tasks"] += 1
            else:
                _mark(conn, m["dm_id"], "error", str(e), attempt=True)
            conn.commit()
            continue
        stored = [_store(conn, owner, m, f"DZ_{m['dm_id']}.zfo", zfo)]
        stored += [_store(conn, owner, m, p["name"], p["data"]) for p in parts]
        _mark(conn, m["dm_id"], "task", f"{len(parts)} příloh", _task(conn, m, stored))
        counts["tasks"] += 1
        conn.commit()
    return {k: v for k, v in counts.items() if v} or {"new": 0}
