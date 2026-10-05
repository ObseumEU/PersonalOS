"""A project's page (pos.project_info): details, decisions, files, activity, summary, auto-attach, weekly routine."""

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, org, project_info, projects, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app


@pytest.fixture
def co(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    c = connect(tmp_path / "p.db")
    migrate(c)
    actors.ensure_builtin(c)
    org.ensure(c)
    agents.seed_builtin_permissions(c)
    me = Ctx(actors.owner_id(c))
    ids = {n: agents.create_agent(c, me, name=n, purpose=n, lifetime="long_lived",
                                  permissions=["tasks:read", "tasks:claim", "tasks:review", "tasks:write"],
                                  data_dir=tmp_path)["agent"]["id"] for n in ("Writer", "Designer")}
    yield c, me, ids
    c.close()


def _kb_docs():
    return [
        {"id": "gh1", "name": "README.md", "origin": "github", "source": "github:ObseumEU/Shop:file:README.md",
         "url": "https://github.com/ObseumEU/Shop/blob/x/README.md", "labels": ["acme"], "workspace": "firma"},
        {"id": "gh2", "name": "src/app.py", "origin": "github", "source": "github:ObseumEU/Shop:file:src/app.py",
         "labels": ["acme"], "workspace": "firma"},
        {"id": "gh3", "name": "PR #7: Pay by card", "origin": "github", "source": "github:ObseumEU/Shop:pull:7",
         "date": "2099-01-01", "url": "https://github.com/ObseumEU/Shop/pull/7", "labels": ["acme"]},
        {"id": "m1", "name": "Nabídka e-shopu", "origin": "mailbox", "source": "mailbox:acme.cz:1",
         "date": "2099-01-02", "channel": "acme.cz", "labels": ["acme"], "url": "https://mail.google.com/x"},
        {"id": "m2", "name": "Jiná věc", "origin": "mailbox", "source": "mailbox:other.cz:2", "date": "2099-01-02",
         "labels": ["other"]},
        {"id": "d1", "name": "Smlouva Acme.pdf", "origin": "gdrive", "source": "gdrive:FOLDER123:f1",
         "date": "2099-01-03", "labels": []},
    ]


def _commits(url, params):
    return [
        {"sha": "abc1234567890", "html_url": "https://github.com/ObseumEU/Shop/commit/abc",
         "commit": {"message": "Add checkout\n\nbody", "author": {"name": "David", "date": "2099-01-04T10:00:00Z"}},
         "author": {"type": "User"}},
        {"sha": "bot", "commit": {"message": "bump", "author": {"name": "renovate[bot]", "date": "2099-01-04T11:00:00Z"}},
         "author": {"type": "Bot"}},
    ]


@pytest.fixture
def kb(monkeypatch):
    monkeypatch.setattr(project_info, "kb_documents_fn", _kb_docs)
    monkeypatch.setattr(project_info, "github_get", _commits)
    project_info._gh_cache.clear()


def test_details_decisions_files_and_activity(co, kb):
    c, me, ids = co
    p = projects.create(c, me, name="Acme e-shop", goal="Launch the shop", lead="Designer", member_refs=["Writer"],
                        details={"description": "# Shop\nFor **Acme**.",
                                 "links": {"repos": ["https://github.com/ObseumEU/Shop.git"],
                                           "drive_folder": "https://drive.google.com/drive/folders/FOLDER123",
                                           "customer": "Acme"},
                                 "facts": {"budget": "120 000 Kč", "stack": "Python"}, "kb_workspace": "acme",
                                 "keywords": ["Acme"]})
    info = p["info"]
    assert info["links"]["repos"] == ["ObseumEU/Shop"] and info["facts"]["budget"] == "120 000 Kč"
    assert info["keywords"] == ["acme"] and info["description"].startswith("# Shop")
    got = projects.update(c, me, p["slug"], {"facts": {"contact": "Jana"}, "goal_progress": 40, "due": "2099-02-01",
                                             "member_roles": {ids["Writer"]: "copywriting"}})
    assert got["info"]["facts"] == {"customer": None, "contact": "Jana", "budget": "120 000 Kč", "stack": "Python"}
    assert got["info"]["goal_progress"] == 40 and got["due"] == "2099-02-01"
    assert next(x for x in got["people"] if x["name"] == "Writer")["project_role"] == "copywriting"
    with pytest.raises(tasks.Invalid):
        projects.update(c, me, p["slug"], {"goal_progress": 140})
    # A new lead: the old one stays a member.
    got = projects.update(c, me, p["slug"], {"lead": "Writer"})
    assert {m["name"]: m["role"] for m in got["members"]} == {"Writer": "lead", "Designer": "member"}
    # Decisions: members log them.
    projects.may_log(c, Ctx(ids["Designer"]), projects._row(c, me, p["slug"]))
    d = project_info.add_log(c, Ctx(ids["Designer"]), p["id"], text="Card payments via Stripe", why="cheapest",
                             date="2099-01-05")
    assert d["who"] == "Designer"
    assert projects.get(c, me, p["slug"])["decisions"][0]["text"] == "Card payments via Stripe"
    # Files: README/docs from the repo, the Drive folder's files, the tagged mail; code files are not listed.
    docs = project_info.project_documents(c, p["id"])
    assert [x["id"] for x in docs["repo_docs"]] == ["gh1"]
    assert [x["id"] for x in docs["drive"]] == ["d1"] and [x["id"] for x in docs["mail"]] == ["m1"]
    projects.update(c, me, p["slug"], {"kb_strict": True})  # a broad workspace: only mail naming the project
    assert project_info.project_documents(c, p["id"])["mail"] == []
    projects.update(c, me, p["slug"], {"kb_strict": False})
    # Activity: commits (no bots), the PR, the mail, the decision and task changes, newest first.
    tasks.create(c, me, {"title": "Design the cart", "project": p["slug"]})
    items = project_info.activity(c, me, projects.get(c, me, p["slug"]), days=30)
    kinds = [i["kind"] for i in items]
    assert {"commit", "pr", "mail", "task_new", "decision"} <= set(kinds)
    assert not any(i["title"] == "bump" for i in items)


def test_summary_is_cached_and_falls_back_without_a_model(co, kb, monkeypatch):
    c, me, ids = co
    p = projects.create(c, me, name="Acme e-shop", lead="Designer")
    tasks.create(c, me, {"title": "Design the cart", "project": p["slug"], "status": "next"})
    calls = []
    monkeypatch.setattr(project_info, "_llm", lambda conn, prompt: calls.append(prompt) or ("Jde to dobře.", None))
    s = project_info.summary(c, me, projects.get(c, me, p["slug"]))
    assert (s["text"], s["source"], s["fresh"]) == ("Jde to dobře.", "llm", True) and len(calls) == 1
    project_info.summary(c, me, projects.get(c, me, p["slug"]))
    assert len(calls) == 1  # unchanged: cached
    # Open tasks moving (status, progress) do not change what the summary says: it settles.
    t0 = projects.get(c, me, p["slug"])["tasks"][0]
    c.execute("UPDATE tasks SET status = 'working', progress = 40 WHERE id = ?", (t0["id"],))
    project_info.summary(c, me, projects.get(c, me, p["slug"]))
    assert len(calls) == 1
    tasks.create(c, me, {"title": "Pick a theme", "project": p["slug"], "status": "next"})
    # A real change waits until the last model summary is SETTLE_S old (one rebuild per half hour).
    assert project_info.summary(c, me, projects.get(c, me, p["slug"]), generate=False)["fresh"] is True
    monkeypatch.setattr(project_info, "SETTLE_S", 0)
    assert project_info.summary(c, me, projects.get(c, me, p["slug"]), generate=False)["fresh"] is False
    monkeypatch.setattr(project_info, "_llm", lambda conn, prompt: None)
    s = project_info.summary(c, me, projects.get(c, me, p["slug"]))
    assert s["source"] == "fallback" and "Hotovo 0 z 2 úkolů" in s["text"]
    assert projects.list_projects(c, me)[0]["summary"] == s["text"]


def test_tasks_for_a_known_repo_or_label_join_the_project(co):
    c, me, ids = co
    shop = projects.create(c, me, name="Acme e-shop", details={"links": {"repos": ["ObseumEU/ShopFront"]},
                                                               "keywords": ["acme"], "kb_workspace": "acme"})
    other = projects.create(c, me, name="Internal", labels=["platform"])
    agent = Ctx(ids["Writer"])
    made = [
        tasks.create(c, agent, {"title": "Fix the checkout", "notes": "https://github.com/ObseumEU/ShopFront/issues/3"}),
        tasks.create(c, agent, {"title": "Deploy tweak", "topic": "platform"}),
        tasks.create(c, agent, {"title": "Call ACME about the invoice"}),
        tasks.create(c, agent, {"title": "Unrelated chore"}),
        tasks.create(c, agent, {"title": "acme again", "topic": "platform"}),  # the label (2) beats a keyword (1)
    ]
    got = {t["title"]: tasks.get(c, me, t["id"])["project_id"] for t in made}
    assert got == {"Fix the checkout": shop["id"], "Deploy tweak": other["id"],
                   "Call ACME about the invoice": shop["id"], "Unrelated chore": None, "acme again": other["id"]}
    projects.update(c, me, shop["slug"], {"status": "done"})
    assert tasks.get(c, me, tasks.create(c, agent, {"title": "acme later"})["id"])["project_id"] is None


def test_weekly_job_records_milestones_flags_stale_projects_and_briefs_the_coo(co, monkeypatch, tmp_path):
    c, me, ids = co
    monkeypatch.setattr(project_info, "_llm", lambda conn, prompt: None)
    monkeypatch.setattr(project_info, "kb_documents_fn", lambda: [])
    monkeypatch.setattr(project_info, "github_get", lambda url, params: [])
    coo = project_info._coo_id(c)  # org.ensure made it
    busy = projects.create(c, me, name="Busy", details={"links": {"repos": ["ObseumEU/Shop"]}})
    t = tasks.create(c, me, {"title": "Ship it", "project": busy["slug"], "status": "next"})
    tasks.complete(c, me, t["id"])
    quiet = projects.create(c, me, name="Quiet")
    out = project_info.weekly_job(c)
    assert out["stale"] == [quiet["slug"]] and out["milestones"] == 1 and out["summaries"] == 2
    assert project_info.log_entries(c, busy["id"])[0]["kind"] == "milestone"
    task = tasks.get(c, me, tasks.parse_id(out["task"]))
    assert task["assignee_id"] == coo and "Quiet" in task["notes"]
    again = project_info.weekly_job(c)
    assert again["milestones"] == 0 and "task" not in again  # once per week


def test_project_page_api(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setattr(project_info, "kb_documents_fn", lambda: [])
    monkeypatch.setattr(project_info, "_llm", lambda conn, prompt: None)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        p = client.post("/api/projects", json={"name": "Web", "description": "Nový web",
                                               "links": {"website": "obseum.cz"}, "kb_workspace": "obseum-interni"}).json()
        assert p["info"]["links"]["website"] == "https://obseum.cz" and p["can_edit"]
        r = client.patch(f"/api/projects/{p['slug']}", json={"description": "Nový web\n\n- a", "facts": {"stack": "React"}})
        assert r.json()["info"]["facts"]["stack"] == "React"
        assert client.patch(f"/api/projects/{p['slug']}", json={"nope": 1}).status_code == 422
        d = client.post(f"/api/projects/{p['slug']}/decisions", json={"text": "Astro, ne Next", "why": "rychlost"}).json()
        assert client.get(f"/api/projects/{p['slug']}/decisions").json()[0]["id"] == d["id"]
        assert client.delete(f"/api/projects/{p['slug']}/decisions/{d['id']}").status_code == 204
        f = client.post("/api/files", files={"file": ("brief.txt", b"hello", "text/plain")}).json()
        assert client.post(f"/api/projects/{p['slug']}/files", json={"file_id": f["id"]}).status_code == 201
        files = client.get(f"/api/projects/{p['slug']}/files").json()
        assert [x["name"] for x in files["files"]] == ["brief.txt"] and files["available"]
        assert client.get(f"/api/projects/{p['slug']}/summary?generate=true").json()["source"] == "fallback"
        client.post("/api/tasks", json={"title": "Homepage", "project": p["slug"]})
        assert client.get(f"/api/projects/{p['slug']}/activity").json()["items"][0]["kind"] == "task_new"
        assert client.delete(f"/api/projects/{p['slug']}/files/{f['id']}").status_code == 204
        assert client.get(f"/api/projects/{p['slug']}/files").json()["files"] == []
