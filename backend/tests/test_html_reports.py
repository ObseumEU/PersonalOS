import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, html_reports, mcp_server, tasks
from pos.config import Settings
from pos.db import connect, migrate
from pos.main import create_app


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


REPORT = {
    "title": "Týden 40 <script>alert(1)</script>", "summary": "Vše běží.", "status": "amber",
    "kpis": [{"label": "Hotové úkoly", "value": 12, "delta": "+3", "status": "green"}],
    "charts": [{"type": "bar", "title": "Úkoly", "labels": ["Po", "Út"], "series": [{"name": "hotovo", "values": [3, 5]}]},
               {"type": "line", "title": "Náklady", "labels": ["Po", "Út", "St"],
                "series": [{"name": "USD", "values": [1.5, -2, 4]}, {"name": "limit", "values": [5, 5, 5]}]}],
    "sections": [{"heading": "Stav", "bullets": ["<img src=x onerror=alert(1)>"]}],
    "next": ["Nasadit T-416"], "asks": ["Schválit rozpočet"],
}


@pytest.fixture(autouse=True)
def no_codex(monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_PUBLIC_URL", "https://pos.example/")


def test_render_escapes_and_draws_both_charts():
    html = html_reports.render(html_reports.validate(REPORT), author="CTO", created="2026-10-01T10:00:00")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img" not in html and "&lt;img src=x" in html
    assert html.count("<svg") == 2 and "<rect" in html and "<polyline" in html
    assert 'lang="cs"' in html and 'name="viewport"' in html
    assert "Co dál" in html and "Co potřebuju od Davida" in html
    assert "http" not in html.replace("https://pos.example", "")  # no CDN, fonts or scripts


@pytest.mark.parametrize("change, field", [
    ({"kpis": [{"label": "k", "value": i} for i in range(6)]}, "kpis"),
    ({"charts": REPORT["charts"] * 2}, "charts"),
    ({"sections": [{"heading": "h", "bullets": ["b"] * 9}]}, "sections"),
    ({"status": "blue"}, "status"),
    ({"charts": [{"type": "bar", "title": "t", "labels": ["a", "b"], "series": [{"name": "s", "values": [1]}]}]},
     "one per label"),
])
def test_limits(change, field):
    with pytest.raises(tasks.Invalid, match=field):
        html_reports.validate({**REPORT, **change})


def test_tool_page_behind_login(tmp_path):
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="s")
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        agent = actors.find_by_name(conn, "Nexus")["id"]
        conn.close()
        server = mcp_server.build(settings.db_path, default_actor=lambda c: agent)

        async def scenario():
            async with Client(server) as c:
                t = _call(await c.call_tool("create_task", {"title": "Weekly", "assignee": "Nexus"}))
                out = _call(await c.call_tool("report_html", {**REPORT, "task_id": t["ref"]}))
                bad = await c.call_tool("report_html", {**REPORT, "kpis": [{"label": "k", "value": 1}] * 6})
                assert bad.is_error
                got = _call(await c.call_tool("get_task", {"task_id": t["ref"]}))
                return out, got

        out, got = anyio.run(scenario)
        assert out["url"] == f"https://pos.example/reports/{out['id']}"
        assert any(out["url"] in a["body"] for a in got["activity"])
        path = f"/reports/{out['id']}"

        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert client.post("/api/auth/login", json={"password": "pw"}).status_code == 200
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "script-src 'none'" in r.headers["content-security-policy"]
        assert "&lt;script&gt;" in r.text and "Nexus" in r.text
        assert client.get("/reports/00000000-0000-4000-8000-000000000000").status_code == 404
        assert client.get("/reports/not-a-uuid").status_code == 404


def test_every_agent_may_use_it(tmp_path):
    conn = connect(tmp_path / "m.db")
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    agents.seed_builtin_permissions(conn)
    assert "report_html" in mcp_server.allowed_tools(conn, ids["Nexus"])
