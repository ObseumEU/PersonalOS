from sentinel import fingerprint as fp
from sentinel.redact import redact


def test_variants_of_one_error_share_a_fingerprint():
    a = "2026-09-26T08:27:09.752Z ERROR run 4411 for user jana@firma.cz failed: timeout after 30.5s (id 3f9c1e2a-1b2c-4d5e-8f90-123456789abc)"
    b = "2026-09-27T01:02:03.001Z ERROR run 17 for user petr@acme.com failed: timeout after 2.0s (id 00000000-1111-2222-3333-444444444444)"
    ca, cb = fp.classify(a), fp.classify(b)
    assert ca["error"] and cb["error"]
    assert fp.fp_of("nexus", ca) == fp.fp_of("nexus", cb)
    assert fp.fp_of("nexus", ca) != fp.fp_of("knowlage", ca)  # per service
    n = fp.normalize(a)
    assert "jana" not in n and "4411" not in n and "3f9c1e2a" not in n and "<email>" in n and "<uuid>" in n


def test_different_errors_differ_and_paths_ips_hex_are_normalized():
    x = fp.classify('ERROR cannot open /data/files/2026/report-17.pdf from 10.0.0.12:5432 sha 9f86d081884c7d659a2feaa0c55ad015')
    y = fp.classify('ERROR cannot open /data/files/2025/other.pdf from 10.0.0.99:5432 sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
    z = fp.classify("ERROR connection refused")
    assert fp.fp_of("s", x) == fp.fp_of("s", y) != fp.fp_of("s", z)


def test_json_logs_by_level_and_status():
    pino_err = '{"level":50,"time":1790411284125,"msg":"db query failed","err":{"type":"PgError","message":"deadlock detected"}}'
    pino_ok = '{"level":30,"time":1,"res":{"statusCode":200},"msg":"request completed"}'
    pino_500 = '{"level":30,"time":1,"res":{"statusCode":503},"msg":"request completed"}'
    litellm_429 = '{"message": "10.0.0.1:44952 - \\"POST /chat/completions HTTP/1.1\\" 429", "level": "INFO"}'
    assert fp.classify(pino_err)["error"] and "PgError" in fp.classify(pino_err)["text"]
    assert not fp.classify(pino_ok)["error"] and fp.classify(pino_ok)["status"] == 200
    assert fp.classify(pino_500)["error"]
    c = fp.classify(litellm_429)
    assert c["status"] == 429 and not c["error"] and c["path"] == "/chat/completions"


def test_access_lines_count_by_route_not_by_id():
    a = fp.classify('INFO:     172.20.0.30:33796 - "GET /api/tasks/123 HTTP/1.0" 500 Internal Server Error')
    b = fp.classify('INFO:     172.20.0.31:1 - "GET /api/tasks/98765 HTTP/1.1" 500 Internal Server Error')
    ok = fp.classify('INFO:     127.0.0.1:34954 - "GET /api/health HTTP/1.1" 200 OK')
    assert a["error"] and fp.fp_of("p", a) == fp.fp_of("p", b) and a["path"] == "/api/tasks/<id>"
    assert not ok["error"] and ok["status"] == 200


def test_not_every_mention_of_error_is_one():
    assert not fp.classify("sync finished: 0 errors")["error"]
    assert not fp.classify("INFO all good")["error"]
    assert fp.classify("Traceback (most recent call last):")["error"]
    assert fp.classify("httpx.ReadTimeout raised: ValueError in parse")["error"]


def test_quota_lines_are_recognized():
    for line in ("You've hit your usage limit. Try again at 5 PM", "openai.RateLimitError: insufficient_quota",
                 "Budget has been exceeded! Current cost: 5.1, Max budget: 5.0"):
        assert fp.QUOTA.search(line)
    assert not fp.QUOTA.search("usage report ready")


def test_redaction_of_secrets_and_personal_data():
    line = ("POST https://api.x.io/v1?api_key=abc123def&x=1 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig123 "
            "key sk-ant-api03-AAAAbbbbCCCCdddd password=hunter2 token: 'pos_abcdefgh12345' from jana.novakova@firma.cz "
            "db postgres://nexus:s3cret@nexus-postgres:5432/nexus ghp_ABCDEFGHIJKLMNOPQRSTUV1234")
    r = redact(line, limit=1000)
    for secret in ("abc123def", "eyJhbGci", "sk-ant-api03", "hunter2", "pos_abcdefgh", "jana.novakova", "s3cret",
                   "ghp_ABCDEF"):
        assert secret not in r, secret
    assert "<email>" in r and "<redacted>" in r


def test_redaction_truncates():
    assert len(redact("x " * 500, limit=300)) == 300


def test_short_hex_ids_and_counters_glued_to_words_share_a_fingerprint():
    same = [("ERROR request 3f2a9c1 failed", "ERROR request 9b8e7d6 failed"),
            ("ERROR job a1b2c3d4 failed", "ERROR job e5f6a7b8 failed"),
            ("ERROR retry3 failed", "ERROR retry4 failed")]
    for a, b in same:
        assert fp.fingerprint("s", a) == fp.fingerprint("s", b), (a, b)
    # plain words made of hex letters stay words
    assert "decade" in fp.normalize("ERROR decade facade failed")
    assert fp.fingerprint("s", "ERROR cache failed") != fp.fingerprint("s", "ERROR table failed")
