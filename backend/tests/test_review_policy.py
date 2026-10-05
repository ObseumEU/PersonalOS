"""T-731: Kniha code reviews go to the Kniha Lead (QA cannot see that repository), never to their
author; the ones already waiting for QA move once; a run that reaches its cost cap hands its task
back with its state instead of failing on error_max_budget_usd."""

import pytest

from pos import agents, review_policy, tasks
from pos.core import Ctx
from test_agent_effectiveness import LEAD, WORKER, _task, env  # noqa: F401 - fixture

pytest.importorskip("pos_worker")
from pos_worker import loop  # noqa: E402
from pos_worker.loop import Worker  # noqa: E402

DONE = "Commit abc1234 na agent/kniha-web.\nOvěřeno: npm run build prošel"


@pytest.fixture
def kniha(env):  # noqa: F811
    conn, ids = env["conn"], env["ids"]
    owner = env["owner"]
    for name, perms, role in (("Kniha Lead", LEAD, "lead"), ("Kniha Developer", WORKER, "developer")):
        made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived",
                                   permissions=list(perms), data_dir=env["db"].parent)
        ids[name] = made["agent"]["id"]
        conn.execute("UPDATE actors SET role = ?, team = 'kniha', reports_to = ? WHERE id = ?",
                     (role, ids["CEO"] if name == "Kniha Lead" else ids["Kniha Lead"], ids[name]))
    conn.commit()
    return env


def _hand_in(conn, ids, assignee, **fields):
    t = _task(conn, Ctx(ids["CEO"]), ids[assignee], title="Opravit formulář", **fields)
    return tasks.complete(conn, Ctx(ids[assignee], via="mcp"), t["id"], DONE)


def test_kniha_code_goes_to_the_kniha_lead_and_other_code_still_to_qa(kniha):
    conn, ids = kniha["conn"], kniha["ids"]
    out = _hand_in(conn, ids, "Kniha Developer")
    assert out["status"] == "review" and out["reviewer_id"] == ids["Kniha Lead"]
    # The topic alone marks Kniha work too (an assignee outside the team).
    out = _hand_in(conn, ids, "Software Engineer", topic="kniha-dev")
    assert out["reviewer_id"] == ids["Kniha Lead"]
    out = _hand_in(conn, ids, "Software Engineer", topic="engineering")
    assert out["reviewer_id"] == ids["QA Reviewer"]


def test_the_kniha_leads_own_code_is_never_its_own_review(kniha):
    conn, ids = kniha["conn"], kniha["ids"]
    out = _hand_in(conn, ids, "Kniha Lead", topic="kniha-dev")
    assert out["status"] == "review" and out["reviewer_id"] == ids["QA Reviewer"]
    assert out["reviewer_id"] != ids["Kniha Lead"]


def test_an_explicit_reviewer_stays(kniha):
    conn, ids = kniha["conn"], kniha["ids"]
    t = _task(conn, Ctx(ids["CEO"]), ids["Kniha Developer"], title="Opravit formulář")
    conn.execute("UPDATE tasks SET reviewer_id = ? WHERE id = ?", (ids["CTO"], t["id"]))
    out = tasks.complete(conn, Ctx(ids["Kniha Developer"], via="mcp"), t["id"], DONE)
    assert out["reviewer_id"] == ids["CTO"]


def test_kniha_reviews_waiting_for_qa_move_once_dry_run_first(kniha):
    conn, ids = kniha["conn"], kniha["ids"]
    qa = ids["QA Reviewer"]

    def waiting(assignee, **kw):
        t = tasks.create(conn, Ctx(ids["CEO"]), {"title": "Čeká", "assignee": {"type": "agent", "id": ids[assignee]},
                                                 **kw})
        conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (qa, t["id"]))
        return t["id"]

    dev, own, other = waiting("Kniha Developer"), waiting("Kniha Lead", topic="kniha-dev"), waiting("Software Engineer")
    conn.commit()
    dry = review_policy.redirect_kniha(conn)
    assert not dry["applied"] and len(dry["to_kniha_lead"]) == 1 and f"T-{dev:03d}→Kniha Lead" in dry["to_kniha_lead"][0]
    assert conn.execute("SELECT reviewer_id FROM tasks WHERE id = ?", (dev,)).fetchone()[0] == qa  # dry
    review_policy.redirect_kniha(conn, apply=True)
    reviewer = {i: conn.execute("SELECT reviewer_id FROM tasks WHERE id = ?", (i,)).fetchone()[0]
                for i in (dev, own, other)}
    assert reviewer == {dev: ids["Kniha Lead"], own: qa, other: qa}
    assert review_policy.redirect_kniha(conn)["to_kniha_lead"] == []  # once: nothing left to move


# ------------------------------------------------------------------ the run's cost cap

class CappedSession:
    """A Claude session that reaches its cost cap after two steps, then answers the wrap-up."""

    def __init__(self, cap):
        self.max_budget_usd = cap
        self.caps, self.prompts = [], []
        self.failed = self.last_message = ""
        self.jsonl = ""

    def run(self, prompt):
        self.caps.append(self.max_budget_usd)
        self.prompts.append(prompt)
        for _ in range(2):
            yield {"type": "item.completed", "item": {"type": "tool_result"}}
        if len(self.prompts) == 1:
            self.last_message = "Upravuji formulář."
            self.failed = loop.BUDGET_MARKER
        else:
            self.last_message = "Hotovo: formulář. Zbývá: test. Další krok: npm run build."

    def stop(self):
        pass


class Client:
    def __init__(self):
        self.handed_back = self.finished = None

    def heartbeat(self, *a, **k):
        return {}

    def inbox(self, run_id=None):
        return []

    def finish_run(self, run_id, status, jsonl, detail=""):
        self.finished = (status, detail)
        return {}

    def task(self, ref):
        return {"assignee_id": 1, "status": "working"}

    def handback(self, ref, note):
        self.handed_back = note
        return {}


def test_a_run_at_its_cost_cap_hands_the_task_back_with_its_state_not_an_error():
    client = Client()
    w = Worker(client, lambda *a: None, sleep=lambda s: None)
    w.me = {"id": 1}
    session = CappedSession(cap=4.0)
    outcome = w._session_loop(session, "go", 7, "T-1")
    assert outcome == "budget"
    assert session.caps == [3.4, 0.6]  # the work stops at 85 %, the wrap-up stays within the cap
    assert session.prompts[1] == loop.BUDGET_WRAPUP
    assert w._after_run("T-1", 7, "claude", session, None, outcome) == "budget"
    assert client.finished[0] == "ok" and loop.BUDGET_MARKER not in client.finished[1]
    assert client.handed_back.startswith("run cost cap reached") and "Další krok: npm run build" in client.handed_back


def test_a_run_without_a_cap_still_fails_as_before():
    client = Client()
    w = Worker(client, lambda *a: None, sleep=lambda s: None)
    w.me = {"id": 1}
    session = CappedSession(cap=None)
    assert w._session_loop(session, "go", 7, "T-1") == "error"
    assert len(session.prompts) == 1
