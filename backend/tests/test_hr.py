from datetime import datetime, timedelta

from pos.hr import (
    AgentRecord,
    AgentStats,
    CreateAgentRequest,
    HRPolicy,
    Lifetime,
    ProposalKind,
    Status,
    decide_over_limit,
    file_weekly_report,
    rate_agent,
    review_agents,
    run_daily_review,
    team_kpis,
    weekly_report,
)
from pos.hr.limits import OverLimitAction
from pos.hr.similarity import purpose_similarity

NOW = datetime(2026, 9, 25, 12, 0)


def agent(id, purpose="", lifetime=Lifetime.LONG_LIVED, age_days=30, idle_days=0, **kw):
    return AgentRecord(
        id=id,
        name=kw.pop("name", id.title()),
        purpose=purpose or f"unique purpose {id}",
        created_by="owner",
        lifetime=lifetime,
        created_at=NOW - timedelta(days=age_days),
        last_active_at=NOW - timedelta(days=idle_days),
        **kw,
    )


def stats(id, done=0, unassisted=None, returned=0, failed=0, open=0, babysit=0, tokens=0):
    return AgentStats(
        agent_id=id,
        tasks_completed=done,
        tasks_completed_unassisted=done if unassisted is None else unassisted,
        tasks_returned=returned,
        tasks_failed=failed,
        tasks_open=open,
        owner_interventions=babysit,
        tokens_used=tokens,
    )


def kinds(result):
    return {(p.kind, p.agent_id) for p in result.proposals}


class FakePlatform:
    def __init__(self, agents, stats):
        self.agents = {a.id: a for a in agents}
        self.stats = stats
        self.archived: list[str] = []
        self.lifetimes: dict[str, Lifetime] = {}
        self.tasks: list[dict] = []

    def list_agents(self):
        return list(self.agents.values())

    def agent_stats(self, agent_id, since, until):
        assert until - since == HRPolicy().window
        return self.stats.get(agent_id, AgentStats(agent_id=agent_id))

    def archive_agent(self, agent_id, reason):
        self.archived.append(agent_id)

    def set_lifetime(self, agent_id, lifetime, reason):
        self.lifetimes[agent_id] = lifetime

    def create_task(self, title, body, assignee, kind="task"):
        self.tasks.append({"title": title, "body": body, "assignee": assignee, "kind": kind})
        return f"t{len(self.tasks)}"


# --- metrics ---------------------------------------------------------------


def test_perfect_agent_scores_one():
    r = rate_agent(stats("a", done=5, tokens=5000))
    assert r.score == 1.0
    assert r.tokens_per_task == 1000


def test_too_little_work_has_no_score():
    assert rate_agent(stats("a", done=2)).score is None


def test_returns_and_babysitting_lower_the_score():
    good = rate_agent(stats("a", done=10))
    returned = rate_agent(stats("b", done=10, returned=5))
    babysat = rate_agent(stats("c", done=10, unassisted=2, babysit=8))
    assert returned.score < good.score
    assert babysat.score < good.score
    assert returned.return_rate == 0.5


def test_score_is_clamped():
    r = rate_agent(stats("a", done=0, failed=5, babysit=20))
    assert r.score == 0.0


def test_team_kpis():
    k = team_kpis([stats("a", done=6, unassisted=3, returned=1, babysit=2), stats("b", done=4)])
    assert k.tasks_completed == 10
    assert k.unassisted_rate == 0.7
    assert k.returned_rate == 0.1
    assert k.owner_interventions == 2


def test_team_kpis_empty():
    k = team_kpis([])
    assert k.unassisted_rate is None and k.returned_rate is None


# --- similarity ------------------------------------------------------------


def test_similarity_matches_czech_and_english_variants():
    assert purpose_similarity("Třídí příchozí e-maily", "třídění příchozích emailů") == 1.0
    assert purpose_similarity("Sorts incoming emails", "sort incoming email") == 1.0
    assert purpose_similarity("Sorts incoming emails", "Fixes GitHub issues") == 0.0
    assert purpose_similarity("", "anything") == 0.0


# --- daily review ----------------------------------------------------------


def test_expired_agent_is_archived():
    a = agent("a", expires_at=NOW - timedelta(hours=1))
    assert kinds(review_agents([a], {}, NOW)) == {(ProposalKind.ARCHIVE_EXPIRED, "a")}


def test_one_shot_done_is_archived_but_not_while_working():
    done = agent("done", lifetime=Lifetime.ONE_SHOT, idle_days=2)
    busy = agent("busy", lifetime=Lifetime.ONE_SHOT, idle_days=2)
    fresh = agent("fresh", lifetime=Lifetime.ONE_SHOT, idle_days=0)
    result = review_agents(
        [done, busy, fresh],
        {"done": stats("done", done=1), "busy": stats("busy", done=1, open=1), "fresh": stats("fresh", done=1)},
        NOW,
    )
    assert kinds(result) == {(ProposalKind.ARCHIVE_DONE, "done")}


def test_idle_long_lived_is_archived():
    result = review_agents([agent("a", idle_days=20), agent("b", idle_days=3)], {}, NOW)
    assert kinds(result) == {(ProposalKind.ARCHIVE_IDLE, "a")}


def test_idle_agent_with_open_work_is_kept():
    result = review_agents([agent("a", idle_days=20)], {"a": stats("a", open=2)}, NOW)
    assert result.proposals == []


def test_ineffective_agent_is_archived_after_grace_period():
    bad = stats("old", done=1, failed=4, babysit=5)
    old = agent("old")
    young = agent("young", age_days=2)
    result = review_agents([old, young], {"old": bad, "young": stats("young", done=1, failed=4, babysit=3)}, NOW)
    assert kinds(result) == {(ProposalKind.ARCHIVE_INEFFECTIVE, "old")}


def test_mediocre_agent_gets_instruction_revision():
    result = review_agents([agent("a")], {"a": stats("a", done=10, returned=4)}, NOW)
    assert kinds(result) == {(ProposalKind.REVISE_INSTRUCTIONS, "a")}


def test_busy_one_shot_is_promoted():
    a = agent("a", lifetime=Lifetime.ONE_SHOT)
    result = review_agents([a], {"a": stats("a", done=3, open=1)}, NOW)
    assert kinds(result) == {(ProposalKind.PROMOTE_LONG_LIVED, "a")}


def test_duplicates_merge_into_the_stronger_agent():
    a = agent("a", purpose="Sorts incoming emails and drafts replies")
    b = agent("b", purpose="Sort incoming email, draft replies")
    c = agent("c", purpose="Fixes GitHub issues labelled agent")
    result = review_agents([a, b, c], {"a": stats("a", done=3, failed=1), "b": stats("b", done=5)}, NOW)
    [merge] = result.proposals
    assert merge.kind is ProposalKind.MERGE
    assert (merge.agent_id, merge.into) == ("a", "b")


def test_system_and_archived_agents_are_left_alone():
    hr = agent("hr", idle_days=90, system=True)
    gone = agent("gone", idle_days=90, status=Status.ARCHIVED)
    result = review_agents([hr, gone], {}, NOW)
    assert result.proposals == []
    assert result.active_before == 1
    assert "hr" in result.ratings


def test_over_capacity_counts_what_review_already_frees():
    policy = HRPolicy(max_active_agents=2)
    busy = [agent(f"a{i}") for i in range(3)]
    over = review_agents(busy, {}, NOW, policy)
    assert (ProposalKind.OVER_CAPACITY, "") in kinds(over)

    freed = review_agents(busy[:2] + [agent("idle", idle_days=30)], {}, NOW, policy)
    assert ProposalKind.OVER_CAPACITY not in {p.kind for p in freed.proposals}


def test_run_daily_review_applies_safe_steps_and_files_tasks():
    platform = FakePlatform(
        [
            agent("idle", idle_days=30),
            agent("oneshot", lifetime=Lifetime.ONE_SHOT),
            agent("sloppy"),
            agent("mail1", purpose="Sorts incoming emails"),
            agent("mail2", purpose="Sort incoming email"),
        ],
        {
            "oneshot": stats("oneshot", done=4, open=1),
            "sloppy": stats("sloppy", done=10, returned=5),
            "mail1": stats("mail1", done=3),
        },
    )
    result = run_daily_review(platform, platform, NOW)
    assert platform.archived == ["idle"]
    assert platform.lifetimes == {"oneshot": Lifetime.LONG_LIVED}
    titles = sorted(t["title"] for t in platform.tasks)
    assert titles == ["Sloučit agenta Mail2 do Mail1", "Upravit instrukce agenta Sloppy"]
    assert all(t["assignee"] == "hr" for t in platform.tasks)
    assert result.active_after == 4
    assert result.task_ids == ["t1", "t2"]


def test_dry_run_changes_nothing():
    platform = FakePlatform([agent("idle", idle_days=30)], {})
    result = run_daily_review(platform, platform, NOW, apply=False)
    assert platform.archived == [] and platform.tasks == []
    assert kinds(result) == {(ProposalKind.ARCHIVE_IDLE, "idle")}


# --- over-limit decisions --------------------------------------------------


def req(purpose, reason="active_limit"):
    return CreateAgentRequest(
        name="New", purpose=purpose, requested_by="dev", lifetime=Lifetime.ONE_SHOT, reason=reason
    )


def test_over_limit_reuses_agent_with_same_purpose():
    agents = [agent("mail", purpose="Sorts incoming emails")]
    d = decide_over_limit(req("sort incoming email"), agents, {}, NOW)
    assert (d.action, d.target_id) == (OverLimitAction.REUSE, "mail")


def test_daily_limit_defers():
    d = decide_over_limit(req("brand new job", reason="daily_limit"), [agent("x")], {}, NOW)
    assert d.action is OverLimitAction.DEFER


def test_active_limit_replaces_weakest_idle_agent():
    agents = [agent("busy"), agent("weak"), agent("dev"), agent("hr", system=True, idle_days=60)]
    s = {
        "busy": stats("busy", done=5, open=2),
        "weak": stats("weak", done=2, failed=4, babysit=4),
        "dev": stats("dev", done=1, failed=9),
    }
    d = decide_over_limit(req("brand new job"), agents, s, NOW)
    assert (d.action, d.target_id) == (OverLimitAction.REPLACE, "weak")


def test_active_limit_asks_owner_when_everyone_works():
    agents = [agent("a"), agent("b")]
    s = {"a": stats("a", done=5, open=1), "b": stats("b", done=5)}
    d = decide_over_limit(req("brand new job"), agents, s, NOW)
    assert d.action is OverLimitAction.ASK_OWNER


# --- weekly report ---------------------------------------------------------


def test_weekly_report_lists_kpis_ratings_and_changes():
    agents = [agent("mail", name="Mail agent"), agent("idle", name="Old helper", idle_days=30)]
    result = review_agents(agents, {"mail": stats("mail", done=8, returned=1, tokens=120000)}, NOW)
    previous = team_kpis([stats("mail", done=4, returned=2)])
    text = weekly_report(result, agents, previous)
    assert text.startswith("# HR přehled 2026-09-25")
    assert "| Dokončené úkoly | 8 | 4 |" in text
    assert "| Tokeny | 120 000 | 0 |" in text
    assert "Mail agent: skóre" in text
    assert "Old helper: archivován (nečinný)" in text


def test_file_weekly_report_creates_read_task_for_owner():
    platform = FakePlatform([], {})
    result = review_agents([], {}, NOW)
    assert file_weekly_report(result, [], platform) == "t1"
    [task] = platform.tasks
    assert task["kind"] == "read" and task["assignee"] == "owner"
    assert "Žádné změny" in task["body"]
