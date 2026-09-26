"""Feedback between colleagues (REVIZE-FUNKCI 3.4)."""

import pytest

from pos import actors, agents, chat, feedback, org, tasks
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "f.db")
    migrate(c)
    actors.ensure_builtin(c)
    org.ensure(c)
    me = Ctx(actors.owner_id(c))
    ids = {}
    for name in ("Writer", "Performance Coach", "Software Engineer"):
        ids[name] = agents.create_agent(c, me, name=name, purpose=name, lifetime="long_lived",
                                        permissions=["tasks:read", "tasks:claim", "messages:send"],
                                        data_dir=tmp_path)["agent"]["id"]
    yield c, me, ids
    c.close()


def test_feedback_reaches_the_agent_and_the_coach_turns_repeats_into_work(team):
    c, me, ids = team
    writer = ids["Writer"]
    t = tasks.create(c, me, {"title": "Newsletter", "assignee": {"type": "agent", "id": writer}})
    fb = feedback.give(c, me, "Writer", "Too long; keep it under 200 words", "critique", t["id"])
    assert fb["to_name"] == "Writer" and fb["task_ref"] == t["ref"] and fb["status"] == "open"
    assert chat.inbox_unread(c, writer) >= 1
    with pytest.raises(tasks.Invalid):
        feedback.give(c, Ctx(writer), "Writer", "I am great")  # feedback is for someone else
    feedback.give(c, Ctx(ids["Software Engineer"]), "Writer", "The subject line was vague", "critique")
    assert [f["body"] for f in feedback.open_for_prompt(c, writer)][0] == "The subject line was vague"
    out = feedback.coach_digest(c)
    assert out["agents"] == 1 and len(out["tasks"]) == 1
    task = tasks.get(c, me, tasks.parse_id(out["tasks"][0]))
    assert task["assignee_name"] == "Performance Coach" and "under 200 words" in task["notes"]
    assert feedback.coach_digest(c)["tasks"] == []  # not twice while the task is open
    # resolving: the coach may, a peer may not; dismissing needs a reason
    with pytest.raises(Forbidden):
        feedback.resolve(c, Ctx(ids["Software Engineer"]), fb["id"], "applied")
    with pytest.raises(tasks.Invalid):
        feedback.resolve(c, Ctx(ids["Performance Coach"]), fb["id"], "dismissed")
    done = feedback.resolve(c, Ctx(ids["Performance Coach"]), fb["id"], "applied", applied_ref=out["tasks"][0])
    assert done["status"] == "applied" and done["applied_ref"] == out["tasks"][0]


def test_instruction_proposals_go_through_the_dev_agent(team):
    c, me, ids = team
    text = "# Writer\n\nWrite short newsletters (under 200 words) with a concrete subject line."
    out = agents.propose_instructions(c, Ctx(ids["Performance Coach"]), ids["Writer"], text, "shorter, clearer")
    t = tasks.get(c, me, tasks.parse_id(out["task"]))
    assert t["assignee_name"] == "Software Engineer" and out["path"] == "agents/writer/INSTRUCTIONS.md"
    assert text in t["notes"]
    with pytest.raises(Forbidden):
        agents.propose_instructions(c, Ctx(ids["Software Engineer"]), ids["Writer"], text)


def test_worker_prompt_shows_open_feedback():
    from pos_worker.prompt import build_task_prompt

    me = {"name": "Writer", "feedback": [{"kind": "critique", "from_name": "Owner", "task_ref": "T-001",
                                          "body": "Too long"}]}
    text = build_task_prompt(me, {"ref": "T-002", "title": "Next newsletter"}, [], include_guardrails=False)
    assert "Recent feedback for you" in text and "critique from Owner (T-001): Too long" in text
