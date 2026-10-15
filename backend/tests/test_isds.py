"""The data box connector (pos.isds, T-638): read only, a new data message → a private task with its ZFO and
attachments as private files and a DM for the owner; developed against stored SOAP answers (note #42)."""

import base64
import json

import httpx

from pos import isds
from pos.credentials import serverstore as ss
from pos.credentials import service as creds
from test_credentials import _nowhere_in_db, app, op  # noqa: F401 - fixtures
from test_credentials_serverstore import store  # noqa: F401 - fixture

PW = "Dat0vka-ctení-heslo-42"
ZFO = b"0\x82signed-zfo-bytes"
PDF = b"%PDF-1.4 rozhodnuti"


def _status(code="0000", text="Provedeno úspěšně."):
    return f"<p:dmStatus><p:dmStatusCode>{code}</p:dmStatusCode><p:dmStatusMessage>{text}</p:dmStatusMessage></p:dmStatus>"


def _soap(op, inner):
    return (f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{isds.SOAP}" xmlns:p="{isds.NS}"><SOAP-ENV:Body>'
            f"<p:{op}Response>{inner}</p:{op}Response></SOAP-ENV:Body></SOAP-ENV:Envelope>")


LIST = _soap("GetListOfReceivedMessages", "<p:dmRecords><p:dmRecord><p:dmID>1234567</p:dmID>"
             "<p:dbIDSender>abc9xyz</p:dbIDSender><p:dmSender>Finanční úřad pro hl. m. Prahu</p:dmSender>"
             "<p:dmAnnotation>Výzva k podání přiznání</p:dmAnnotation><p:dmSenderRefNumber>12/2026</p:dmSenderRefNumber>"
             "<p:dmPersonalDelivery>false</p:dmPersonalDelivery><p:dmDeliveryTime>2026-10-15T08:00:00.000+02:00"
             "</p:dmDeliveryTime><p:dmAcceptanceTime>2026-10-15T08:20:00.000+02:00</p:dmAcceptanceTime>"
             "</p:dmRecord></p:dmRecords>" + _status())
SIGNED = _soap("SignedMessageDownload", f"<p:dmSignature>{base64.b64encode(ZFO).decode()}</p:dmSignature>" + _status())
MESSAGE = _soap("MessageDownload", "<p:dmReturnedMessage><p:dmDm><p:dmFiles>"
                f'<p:dmFile dmMimeType="application/pdf" dmFileDescr="vyzva.pdf"><p:dmEncodedContent>'
                f"{base64.b64encode(PDF).decode()}</p:dmEncodedContent></p:dmFile>"
                "</p:dmFiles></p:dmDm></p:dmReturnedMessage>" + _status())


def _register(conn, owner):
    ss.put(isds.CREDENTIAL, ss.basic_fields("cteni-konektor", PW), source="test")
    creds.add(conn, owner, {"name": isds.CREDENTIAL, "op_ref": f"pos://{isds.CREDENTIAL}/authorization",
                            "header": "Authorization: Basic {value}", "allowed_hosts": [isds.HOST],
                            "allowed_tools": ["http"]})


def _transport(calls, answers):
    def handler(request: httpx.Request):
        body = request.content.decode()
        op = next(o for o in answers if f"<v20:{o}>" in body)
        calls.append((op, str(request.url), request.headers.get("authorization")))
        status, text = answers[op]
        return httpx.Response(status, text=text)

    return httpx.MockTransport(handler)


def _dms(conn, actor_id):
    return [r["body"] for r in conn.execute(
        "SELECT m.body FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id WHERE i.actor_id = ?", (actor_id,))]


def test_without_the_credential_the_job_skips_and_calls_nothing(app):  # noqa: F811
    calls = []
    out = isds.poll(app["conn"], transport=_transport(calls, {}))
    assert "skipped" in out and calls == []


def test_a_new_message_becomes_a_private_task_with_its_files_and_a_dm_once(app, store):  # noqa: F811
    conn, owner = app["conn"], app["owner"]
    _register(conn, owner)
    calls = []
    t = _transport(calls, {"GetListOfReceivedMessages": (200, LIST), "SignedMessageDownload": (200, SIGNED),
                           "MessageDownload": (200, MESSAGE)})
    assert isds.poll(conn, transport=t) == {"tasks": 1}
    assert [c[0] for c in calls] == ["GetListOfReceivedMessages", "SignedMessageDownload", "MessageDownload"]
    assert calls[0][1] == isds.INFO_URL and calls[1][1] == isds.OPS_URL
    assert {c[2] for c in calls} == {"Basic " + base64.b64encode(f"cteni-konektor:{PW}".encode()).decode()}
    task = conn.execute("SELECT * FROM tasks WHERE source = 'isds'").fetchone()
    assert task["assignee_id"] == owner.actor_id and task["visibility"] == "private" and task["priority"] == 1
    assert "Výzva k podání přiznání" in task["title"] and "<external" in task["notes"]
    fs = conn.execute("SELECT * FROM files WHERE origin = 'isds' ORDER BY id").fetchall()
    assert [f["name"] for f in fs] == ["DZ_1234567.zfo", "vyzva.pdf"]
    assert {f["visibility"] for f in fs} == {"private"}  # Ú6: never pushed into knowlage
    assert all(f"/files?file={f['id']}" in task["notes"] for f in fs)
    assert any("Výzva k podání přiznání" in b for b in _dms(conn, owner.actor_id))
    # The next run: the same message is not downloaded or announced again.
    calls.clear()
    assert isds.poll(conn, transport=t) == {"new": 0}
    assert [c[0] for c in calls] == ["GetListOfReceivedMessages"]
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source = 'isds'").fetchone()[0] == 1
    _nowhere_in_db(app["db"], PW)


def test_a_download_that_keeps_failing_still_gets_a_task_without_files(app, store):  # noqa: F811
    conn, owner = app["conn"], app["owner"]
    _register(conn, owner)
    refused = _soap("SignedMessageDownload", _status("1222", "Zpráva do vlastních rukou"))
    t = _transport([], {"GetListOfReceivedMessages": (200, LIST), "SignedMessageDownload": (200, refused)})
    for _ in range(isds.MAX_ATTEMPTS - 1):
        assert isds.poll(conn, transport=t) == {"errors": 1}
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source = 'isds'").fetchone()[0] == 0
    assert isds.poll(conn, transport=t) == {"tasks": 1, "errors": 1}
    task = conn.execute("SELECT * FROM tasks WHERE source = 'isds'").fetchone()
    assert "1222" in task["notes"] and "žádné" in task["notes"]
    assert isds.poll(conn, transport=t) == {"new": 0}


def test_a_refused_login_is_an_error_and_makes_nothing(app, store):  # noqa: F811
    conn, owner = app["conn"], app["owner"]
    _register(conn, owner)
    out = isds.poll(conn, transport=_transport([], {"GetListOfReceivedMessages": (401, "")}))
    assert out["errors"] == 1 and "401" in out["error"] and PW not in json.dumps(out)
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source = 'isds'").fetchone()[0] == 0
