"""One definition of the review queue (pos.review_queue), the same on Úkoly, Firma and the weekly review."""

from fastapi.testclient import TestClient

from pos import actors, review_queue, scorecard, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app


def _conn(tmp_path):
    c = connect(tmp_path / "q.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return c


def _review(conn, owner, reviewer_id):
    t = tasks.create(conn, owner, {"title": "Výsledek", "status": "next"})
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (reviewer_id, t["id"]))


def test_queue_says_whose_it_is(tmp_path):
    conn = _conn(tmp_path)
    owner_id = actors.owner_id(conn)
    owner = Ctx(owner_id)
    lead = conn.execute("SELECT id FROM actors WHERE id != ? ORDER BY id LIMIT 1", (owner_id,)).fetchone()["id"]
    for _ in range(3):
        _review(conn, owner, lead)
    _review(conn, owner, None)  # from before reviewers: the owner's
    conn.commit()
    q = review_queue.queue(conn)
    assert (q["total"], q["for_owner"], q["for_leads"]) == (4, 1, 3)
    assert q["text"] == "3 čekají na kontrolu vedoucích, 1 na tebe"
    # Firma reads the same numbers.
    a = scorecard.agents_health(conn)
    assert a["review_queue"] == 4 and a["review_text"] == q["text"]


def test_empty_queue_and_plural():
    assert review_queue.text(0, 0).startswith("Nic nečeká")
    assert review_queue.text(70, 0) == "70 čeká na kontrolu vedoucích, 0 na tebe"


def test_endpoint_and_weekly_review_share_it(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as c:
        q = c.get("/api/review-queue").json()
        assert q["total"] == 0 and "text" in q
        assert c.get("/api/weekly-review").json()["review_queue"] == q
