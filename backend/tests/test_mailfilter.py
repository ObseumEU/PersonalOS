import json

from pos import actors, mailfilter, routing
from pos.core import Ctx
from pos.db import connect, migrate


def mail(**headers):
    labels = headers.pop("labels", [])
    return {"source": "gmail", "kind": "email", "ref": headers.pop("ref", "m1"), "title": "Hello",
            "author": headers.get("From", "Jana <jana@client.cz>"), "meta": {"headers": headers, "labels": labels}}


def test_rules_skip_bulk_and_automatic_mail_but_not_people():
    r = mailfilter.skip_reason
    assert r(mail(From="Jana Nováková <jana@client.cz>")) is None
    assert "mailing list" in r(mail(**{"List-Unsubscribe": "<mailto:u@x>"}))
    assert "mailing list" in r(mail(**{"List-Id": "news.example.com"}))
    assert r(mail(Precedence="bulk")) == "precedence bulk"
    assert "automatic sender" in r(mail(From="GitHub <noreply@github.com>"))
    assert "automatic sender" in r(mail(From="notifications@slack.com"))
    assert "automatic sender" in r(mail(From="MAILER-DAEMON@mx.google.com"))
    assert r(mail(labels=["INBOX", "CATEGORY_PROMOTIONS"])) == "Gmail category promotions"
    assert "auto-submitted" in r(mail(**{"Auto-Submitted": "auto-replied"}))
    assert r(mail(**{"Auto-Submitted": "no"})) is None
    # Gmail API header list form
    ev = {"source": "gmail", "meta": {"headers": [{"name": "List-Id", "value": "x"}]}}
    assert "mailing list" in r(ev)
    assert r({"source": "github", "meta": {"headers": {"List-Id": "x"}}}) is None  # only mail


def test_rules_are_configurable(tmp_path, monkeypatch):
    cfg = tmp_path / "mail.json"
    cfg.write_text(json.dumps({"senders": [r"@newsletter\.cz$"], "categories": []}))
    monkeypatch.setenv("POS_MAIL_PREFILTER", str(cfg))
    assert mailfilter.skip_reason(mail(labels=["CATEGORY_PROMOTIONS"])) is None
    assert "automatic sender" in mailfilter.skip_reason(mail(From="info@newsletter.cz"))
    monkeypatch.setenv("POS_MAIL_PREFILTER", "off")
    assert mailfilter.skip_reason(mail(Precedence="bulk")) is None


def test_skipped_mail_is_counted_and_starts_no_task(tmp_path):
    c = connect(tmp_path / "m.db")
    migrate(c)
    actors.ensure_builtin(c)
    routing.seed_defaults(c)
    ctx = Ctx(actors.owner_id(c))
    news = routing.ingest(c, ctx, mail(ref="n1", Precedence="bulk"))
    assert news["skipped"] and news["task_id"] is None
    person = routing.ingest(c, ctx, mail(ref="p1", From="jana@client.cz"))
    assert person.get("task_id")
    again = routing.ingest(c, ctx, mail(ref="n1", Precedence="bulk"))
    assert again["duplicate"]  # never processed twice
    assert routing.skipped_mail(c)["total"] == 1
    assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
