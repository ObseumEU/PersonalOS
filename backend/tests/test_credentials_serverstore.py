"""The server-side credential store (pos.credentials.serverstore): the second backend next to 1Password.

Values encrypted at rest, written only by the operator's CLI, used by agents exactly like 1Password credentials
(the same grants, hosts, audit, redaction), by the reality probe, and accepted by the grounding check."""

import base64
import json
import os

import httpx
import pytest

from pos import grounding
from pos.config import get_settings
from pos.core import Ctx
from pos.credentials import __main__ as cli
from pos.credentials import serverstore as ss
from pos.credentials import service as creds
from test_credentials import _nowhere_in_db, app, op  # noqa: F401 - fixtures

PW = "Kn1ha-T3st-heslo-xyz"
ENV = f"# kniha-test\nPRISTUP_HESLO={PW}\nADMIN_HESLO='adm-Heslo-987654'\nOTHER=1\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POS_SECRETS_KEY", "k" * 40)
    get_settings.cache_clear()
    yield tmp_path / "secrets" / "credentials"
    get_settings.cache_clear()


def _put_from_env(monkeypatch, capsys, *args):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(ENV))
    rc = cli.main(["put", *args])
    out = capsys.readouterr()
    assert PW not in out.out + out.err and "adm-Heslo" not in out.out + out.err
    return rc, out.out


def test_cli_stores_basic_auth_fields_encrypted_and_never_prints_a_value(store, monkeypatch, capsys):
    rc, out = _put_from_env(monkeypatch, capsys, "kniha-test-basic-auth", "--from-env-file", "-", "--key",
                            "PRISTUP_HESLO", "--basic-user", "tester")
    assert rc == 0 and "authorization,password,username" in out
    blob = (store / "kniha-test-basic-auth.bin").read_bytes()
    assert PW.encode() not in blob and base64.b64encode(f"tester:{PW}".encode()) not in blob
    if os.name != "nt":
        assert (store / "kniha-test-basic-auth.bin").stat().st_mode & 0o077 == 0
    assert ss.resolve("pos://kniha-test-basic-auth/password") == PW
    assert ss.resolve("pos://kniha-test-basic-auth/authorization") == base64.b64encode(f"tester:{PW}".encode()).decode()
    # Quotes in the env file are stripped; list shows names and fields only.
    rc, _ = _put_from_env(monkeypatch, capsys, "kniha-test-admin", "--from-env-file", "-", "--key", "ADMIN_HESLO",
                          "--basic-user", "admin")
    assert rc == 0 and ss.resolve("pos://kniha-test-admin/password") == "adm-Heslo-987654"
    assert cli.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "kniha-test-admin\tfields=authorization,password,username" in listed and "adm-Heslo" not in listed
    # A missing key, an entry or field that does not exist: refused, with words the grounding check reads.
    assert _put_from_env(monkeypatch, capsys, "x-y", "--from-env-file", "-", "--key", "NOPE")[0] == 2
    with pytest.raises(ss.Unavailable, match="no such item"):
        ss.resolve("pos://nothing-here/password")
    with pytest.raises(ss.Unavailable, match="no field"):
        ss.resolve("pos://kniha-test-admin/token")


def test_another_key_cannot_read_an_entry(store, monkeypatch):
    ss.put("some-cred", {"password": "v4lue-123456"})
    monkeypatch.setenv("POS_SECRETS_KEY", "z" * 40)
    with pytest.raises(ss.Unavailable, match="cannot be decrypted"):
        ss.resolve("pos://some-cred/password")


def test_agents_use_a_server_store_credential_like_a_1password_one(app, store, monkeypatch):  # noqa: F811
    conn, owner, agent = app["conn"], app["owner"], app["agent"]
    ss.put("kniha-test-admin", ss.basic_fields("admin", PW), source="test")
    # 1Password off entirely: the server store still works (each credential's own backend decides).
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN")
    with pytest.raises(creds.CredentialError, match="pos://"):
        creds.add(conn, owner, {"name": "bad", "op_ref": "pos://Bad Name/x"})
    creds.add(conn, owner, {"name": "kniha-test-admin", "op_ref": "pos://kniha-test-admin/authorization",
                            "header": "Authorization: Basic {value}", "allowed_hosts": ["kniha-test.obseum.cz"],
                            "allowed_tools": ["http", "browser"]})
    with pytest.raises(creds.CredentialError, match="no active grant"):
        creds.http_call(conn, Ctx(agent), "GET", "https://kniha-test.obseum.cz/admin", ["kniha-test-admin"])
    creds.grant(conn, owner, agent, "kniha-test-admin", "live checks")
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text=f"hello {PW} {request.headers.get('authorization')}")

    out = creds.http_call(conn, Ctx(agent), "GET", "https://kniha-test.obseum.cz/admin", ["kniha-test-admin"],
                          transport=httpx.MockTransport(handler))
    assert seen["auth"] == "Basic " + base64.b64encode(f"admin:{PW}".encode()).decode()
    assert out["status"] == 200 and PW not in json.dumps(out) and "[REDACTED:kniha-test-admin]" in out["body"]
    with pytest.raises(creds.CredentialError, match="not allowed"):
        creds.http_call(conn, Ctx(agent), "GET", "https://evil.example.com/", ["kniha-test-admin"],
                        transport=httpx.MockTransport(handler))
    uses = conn.execute("SELECT ok FROM credential_uses WHERE name = 'kniha-test-admin' ORDER BY id").fetchall()
    assert [u["ok"] for u in uses] == [0, 1, 0]
    # The platform's own probe and the owner's test resolve it too; the check says ok.
    assert creds.platform_header(conn, owner, "kniha-test-admin", "kniha-test.obseum.cz", "probe")[0] == "Authorization"
    assert creds.test(conn, owner, creds.get(conn, "kniha-test-admin")["id"])["ok"] is True
    assert creds.check_reference(conn, name="kniha-test-admin")["state"] == "ok"
    ss.delete("kniha-test-admin")
    assert creds.check_reference(conn, name="kniha-test-admin")["state"] == "missing"
    with pytest.raises(creds.CredentialError, match="server credential store unavailable"):
        creds.http_call(conn, Ctx(agent), "GET", "https://kniha-test.obseum.cz/admin", ["kniha-test-admin"],
                        transport=httpx.MockTransport(handler))
    _nowhere_in_db(app["db"], PW)


def test_grounding_accepts_server_store_entries_and_refs(app, store, monkeypatch):  # noqa: F811
    conn = app["conn"]
    grounding._cred_cache.clear()
    ss.put("kniha-test-data-klic", {"password": "d4ta-kl1c-0123456789"})
    ok = grounding.credential_findings(conn, "Klíč je v úložišti na serveru jako `kniha-test-data-klic`, reference "
                                             "`pos://kniha-test-data-klic/password`.")
    assert ok == []
    bad = grounding.credential_findings(conn, "Heslo najdeš v `pos://kniha-test-nic/password`.")
    assert bad and "úložišti na serveru" in bad[0].reason


def test_a_basic_auth_value_also_redacts_its_password_alone():
    from pos.credentials.redact import Redactor, basic_auth_password

    value = base64.b64encode(f"tester:{PW}".encode()).decode()
    assert basic_auth_password(value) == PW and basic_auth_password("Basic " + value) == PW
    assert basic_auth_password("ghp_S3cr3tV4lue0123456789") is None
    r = Redactor({"kniha-test-basic-auth": value})
    assert PW not in r(f"echo {PW} and {value}") and r(f"x {PW}") == "x [REDACTED:kniha-test-basic-auth]"
