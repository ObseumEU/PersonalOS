"""Read-then-insert races: another request creates the row between our read and our insert.

Each test hides the row from the first matching read (`_Raced`), so the code takes its "missing" branch
while the row is already there, which is what a concurrent request does to it. It must neither fail
on the unique key (sqlite3.IntegrityError) nor leave a second row."""

import pytest

from pos import actors
from pos.core import Ctx
from pos.db import connect, migrate


class _Raced:
    """The connection, except that the first read containing `needle` finds nothing."""

    def __init__(self, conn, needle: str, times: int = 1):
        self._conn, self._needle, self._left = conn, needle, times

    def execute(self, sql, params=()):
        if self._left and self._needle in sql and sql.lstrip().upper().startswith("SELECT"):
            self._left -= 1
            return self._conn.execute("SELECT 1 WHERE 0")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "personalos.db")
    migrate(c)
    actors.ensure_builtin(c)
    yield c
    c.close()


def _count(conn, sql, *args) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {sql}", args).fetchone()["n"]


def test_system_member_created_by_a_concurrent_request(conn):
    sid = actors.system_id(conn)
    conn.commit()
    assert actors.system_id(_Raced(conn, "FROM actors WHERE name")) == sid
    assert _count(conn, "actors WHERE name = ?", actors.SYSTEM_NAME) == 1


def test_builtin_members_created_by_a_concurrent_start(conn):
    ids = actors.ensure_builtin(conn)
    assert actors.ensure_builtin(_Raced(conn, "SELECT id FROM actors WHERE name")) == ids
    assert _count(conn, "actors") == len(ids)


def _owner(conn) -> Ctx:
    return Ctx(actors.owner_id(conn))


def test_topic_created_by_a_concurrent_request(conn):
    from pos import tasks, topics

    owner = _owner(conn)
    tasks.create(conn, owner, {"title": "x", "topic": "racing"})
    topics.update(conn, owner, "racing", {"description": "one"})
    topics.update(_Raced(conn, "FROM topics WHERE slug"), owner, "racing", {"description": "two"})
    assert _count(conn, "topics WHERE slug = 'racing'") == 1
    with pytest.raises(topics.Invalid):
        topics.create(_Raced(conn, "FROM topics WHERE slug"), owner, {"name": "racing"})


def test_notice_digest_line_added_by_a_concurrent_notice(conn):
    from pos import notice_digest
    from pos.notices import system_ctx

    to = actors.find_by_name(conn, "Nexus")
    notice_digest.add(conn, system_ctx(conn), to, "T-1 waits for your review.", "review_waits", None)
    notice_digest.add(_Raced(conn, "FROM notice_digest_items WHERE actor_id"), system_ctx(conn), to,
                      "T-1 waits for your review.", "review_waits", None)
    rows = conn.execute("SELECT count FROM notice_digest_items WHERE actor_id = ?", (to["id"],)).fetchall()
    assert [r["count"] for r in rows] == [2]


def test_deploy_review_asked_twice_at_once_makes_one_task(conn, tmp_path):
    from pos import agents, deploy_review

    agents.create_agent(conn, _owner(conn), name="QA Reviewer", purpose="reviews", lifetime="long_lived",
                        permissions=["tasks:read", "tasks:claim", "tasks:review"], data_dir=tmp_path)
    dep = Ctx(actors.find_by_name(conn, "Deployer")["id"], via="deployer")
    sha = "b" * 40
    first = deploy_review.ask(conn, dep, sha, author="x")
    again = deploy_review.ask(_Raced(conn, "FROM deploy_reviews WHERE sha"), dep, sha, author="x")
    assert first["status"] == again["status"] == "pending" and again["task"] == first["task"]
    assert _count(conn, "tasks WHERE title LIKE 'Review agent/dev%'") == 1


def test_reality_capability_added_by_a_concurrent_call(conn):
    from pos import projects, reality

    owner = _owner(conn)
    p = projects.create(conn, owner, name="Race shop")
    reality.upsert(conn, owner, p["slug"], "login", {"name": "Přihlášení"})
    out = reality.upsert(_Raced(conn, "FROM reality_capabilities WHERE project_id = ? AND key = ?"), owner,
                         p["slug"], "login", {"name": "Přihlášení e-mailem"})
    assert out["name"] == "Přihlášení e-mailem"
    assert _count(conn, "reality_capabilities WHERE key = 'login'") == 1


def test_an_invitation_accepted_twice_at_once_makes_one_person(conn, monkeypatch):
    from pos import accounts

    inv = accounts.invite(conn, _owner(conn), email="eva@firma.cz", name="Eva")
    stale = accounts._invite_row(conn, inv["token"])  # what the second submit read: still unused
    accounts.accept(conn, inv["token"], "eva-password-1")
    monkeypatch.setattr(accounts, "_invite_row", lambda c, token: stale)
    with pytest.raises(accounts.AuthError):
        accounts.accept(conn, inv["token"], "eva-password-1")
    conn.rollback()
    assert _count(conn, "human_accounts WHERE email = 'eva@firma.cz'") == 1
    assert _count(conn, "actors WHERE name = 'Eva'") == 1
