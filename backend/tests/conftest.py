import pytest


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    """Tests never read the machine's real Codex sessions (~/.codex)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
