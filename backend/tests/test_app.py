import pytest
from fastapi.testclient import TestClient

from pos.config import Settings
from pos.db import MIGRATIONS, connect, migrate
from pos.main import create_app


def make_client(tmp_path, **overrides) -> TestClient:
    settings = Settings(data_dir=tmp_path, **overrides)
    return TestClient(create_app(settings))


def test_health_is_public(tmp_path):
    with make_client(tmp_path, password="pw", session_secret="s") as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_startup_creates_db_and_files_dir(tmp_path):
    with make_client(tmp_path):
        assert (tmp_path / "personalos.db").exists()
        assert (tmp_path / "files").is_dir()


def test_login_flow(tmp_path):
    with make_client(tmp_path, password="pw", session_secret="s") as client:
        assert client.get("/api/auth/me").json() == {
            "authenticated": False,
            "login_required": True,
        }
        assert client.get("/api/system").status_code == 401
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401

        assert client.post("/api/auth/login", json={"password": "pw"}).status_code == 200
        assert client.get("/api/auth/me").json()["authenticated"] is True
        assert client.get("/api/system").status_code == 200

        client.post("/api/auth/logout")
        assert client.get("/api/system").status_code == 401


def test_no_password_means_open_dev_mode(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/api/auth/me").json() == {
            "authenticated": True,
            "login_required": False,
        }
        assert client.get("/api/system").status_code == 200


def test_password_requires_real_session_secret(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=tmp_path, password="pw"))


def test_migrations_are_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert migrate(conn) == len(MIGRATIONS)
    assert migrate(conn) == len(MIGRATIONS)
    conn.close()
