"""Attached images reach the model: a screenshot in chat, an answer or a comment is downloaded into
the run's folder and the prompt says where (pos_worker.images); prod 2026-10-05: the CEO could not
read the owner's screenshot (soubor #14), the MCP tools only give a file's text."""

import base64
import io
import sys

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("pos_worker")
from pos_worker import images  # noqa: E402
from pos_worker.claude import ClaudeSession  # noqa: E402
from pos_worker.client import PosClient  # noqa: E402
from pos_worker.codex import CodexSession  # noqa: E402
from pos_worker.loop import Worker  # noqa: E402

from pos import actors, agents, files, tasks  # noqa: E402
from pos.config import Settings  # noqa: E402
from pos.core import Ctx  # noqa: E402
from pos.db import connect  # noqa: E402
from pos.main import create_app  # noqa: E402

# A 1×1 PNG.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

# The fake CLI checks that the image's folder is readable (--add-dir), that Read exists, and that the
# prompt names a path that holds the image's bytes; its answer says what it "saw".
FAKE_CLAUDE = r'''
import json, os, re, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
sid = "sess-img"
def out(ev):
    print(json.dumps(ev), flush=True)
seen = "none"
m = re.search(r"soubor #\d+: (\S+)", prompt)
if m and "--add-dir" in args:
    path = m.group(1)
    folder = args[args.index("--add-dir") + 1]
    tools = args[args.index("--tools") + 1] if "--tools" in args else ""
    if os.path.isfile(path) and os.path.dirname(os.path.abspath(path)) == os.path.abspath(folder) and "Read" in tools:
        seen = "PNG" if open(path, "rb").read(4) == b"\x89PNG" else "other"
out({"type": "system", "subtype": "init", "session_id": sid, "model": "m"})
out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": "seen:" + seen}]}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "seen:" + seen,
     "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 0}})
'''


def _wrap(tmp_path, code):
    script = tmp_path / "fake_claude.py"
    script.write_text(code)
    if sys.platform == "win32":
        cmd = tmp_path / "claude.cmd"
        cmd.write_text(f'@set PYTHONUTF8=1\r\n@"{sys.executable}" "{script}" %*\r\n')
        return str(cmd)
    sh = tmp_path / "claude"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    sh.chmod(0o755)
    return str(sh)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_AGENT_RUNTIME", "claude")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    out = agents.create_agent(conn, owner, name="Software Engineer", purpose="code", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "approvals:request"], data_dir=tmp_path)
    yield client, conn, owner, out["agent"]["id"], out["api_key"], settings
    conn.close()
    client.__exit__(None, None, None)


def _upload(conn, owner, settings, data: bytes, name: str, visibility: str | None = None) -> dict:
    f = files.upload(conn, owner, settings.files_dir, io.BytesIO(data), name, visibility=visibility)
    conn.commit()
    return f


def test_file_refs_are_found_once_in_order():
    text = "opravte podle\n📎 12865.png (soubor #14)\nViz soubor #3, file #14 a obrázek # 7; T-12 ne."
    assert images.file_ids(text) == [14, 3, 7]
    assert images.file_ids("bez příloh") == []


def test_worker_api_gives_only_readable_images(setup):
    client, conn, owner, agent_id, key, settings = setup
    img = _upload(conn, owner, settings, PNG, "shot.png")
    txt = _upload(conn, owner, settings, b"hello", "notes.txt")
    secret = _upload(conn, owner, settings, PNG + b"x", "private.png", visibility="private")
    h = {"Authorization": f"Bearer {key}"}
    r = client.get(f"/api/worker/files/{img['id']}/image", headers=h)
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-file-name"] == "shot.png"
    assert client.get(f"/api/worker/files/{txt['id']}/image", headers=h).status_code == 415
    assert client.get(f"/api/worker/files/{secret['id']}/image", headers=h).status_code in (403, 404)
    assert client.get(f"/api/worker/files/{img['id']}/image").status_code == 401


def test_a_screenshot_in_the_task_reaches_the_model(setup, tmp_path):
    client, conn, owner, agent_id, key, settings = setup
    img = _upload(conn, owner, settings, PNG, "12865.png")
    t = tasks.create(conn, owner, {"title": "Opravit podle screenshotu", "assignee": {"type": "agent", "id": agent_id},
                                   "notes": f"Majitel napsal „opravte podle“.\n📎 12865.png (soubor #{img['id']})"})
    conn.commit()
    binary = _wrap(tmp_path, FAKE_CLAUDE)
    sessions = []

    def new_session(engine, model, me):
        s = ClaudeSession(binary=binary, workdir=str(tmp_path), model=model, system_prompt="rules",
                          builtin_tools=["Glob", "Grep"], allowed_tools=["mcp__pos"],
                          mcp_servers={"pos": {"type": "http", "url": "http://x/mcp"}})
        sessions.append(s)
        return s

    worker = Worker(PosClient("http://testserver", key, http=client), new_session, poll_wait=0,
                    sleep=lambda s: None, images_dir=str(tmp_path / "imgs"))
    assert worker.step() == "ok"
    done = tasks.get(conn, owner, t["id"])
    assert "seen:PNG" in done["progress_note"]
    s = sessions[0]
    assert "Read" in s.builtin_tools and "Read" in s.allowed_tools and s.add_dirs
    assert not (tmp_path / "imgs").exists() or not any((tmp_path / "imgs").iterdir())  # removed after the run


def test_no_reference_no_folder_and_codex_gets_image_flags(tmp_path):
    class Http:
        def __init__(self):
            self.calls = []

        def get(self, path):
            self.calls.append(path)

            class R:
                status_code = 200
                headers = {"content-type": "image/png", "x-file-name": "a%20b.png"}
                content = PNG
            return R()

    client = type("C", (), {"http": Http()})()
    ri = images.RunImages(client, tmp_path / "run-1")
    assert ri.fetch("nothing attached") == [] and not (tmp_path / "run-1").exists()
    got = ri.fetch("📎 a b.png (soubor #5) and again soubor #5")
    assert [fid for fid, _ in got] == [5] and got[0][1].read_bytes() == PNG and client.http.calls == ["/api/worker/files/5/image"]
    assert ri.fetch("soubor #5") == []  # once per run
    assert "Read" in images.note(got) and got[0][1].as_posix() in images.note(got)
    cx = CodexSession(binary="codex", workdir=str(tmp_path))
    cx.attach_images(ri.folder, [p for _, p in got])
    assert f"--image={got[0][1]}" in cx._args(resume=False) and cx._args(resume=False)[-1] == "-"
    ri.cleanup()
    assert not (tmp_path / "run-1").exists()


def test_comment_and_answer_carry_attachments(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        up = client.post("/api/files", files={"file": ("snimek.png", PNG, "image/png")}).json()
        t = client.post("/api/tasks", json={"title": "Check the page"}).json()
        r = client.post(f"/api/tasks/{t['ref']}/comments", json={"body": "Tohle je rozbité", "attachments": [up["id"]]})
        assert r.status_code == 201 and r.json()["body"] == f"Tohle je rozbité\n📎 snimek.png (soubor #{up['id']})"
        r = client.post(f"/api/tasks/{t['ref']}/comments", json={"body": "", "attachments": [up["id"]]})
        assert r.status_code == 201 and r.json()["body"] == f"📎 snimek.png (soubor #{up['id']})"
        assert client.post(f"/api/tasks/{t['ref']}/comments", json={"body": "  "}).status_code in (400, 422)
        assert client.post(f"/api/tasks/{t['ref']}/comments", json={"body": "x", "attachments": [999999]}).status_code == 404
        done = client.post(f"/api/tasks/{t['ref']}/complete", json={"note": "Hotovo", "attachments": [up["id"]]}).json()
        assert done["progress_note"].endswith(f"(soubor #{up['id']})")
