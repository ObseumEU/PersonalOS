import pytest


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    """Tests never read the machine's real Codex sessions (~/.codex) and never
    call a real AI engine; tests that need one bring a fake binary."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_CLAUDE_DISABLED", "1")
    # Nor the real knowlage: without a key, files are not pushed and search uses file names.
    monkeypatch.delenv("POS_KNOWLAGE_API_KEY", raising=False)
    # Tests create their own agents; the role agents from agents/*/agent.json only where a test asks.
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "0")
    monkeypatch.delenv("POS_WORKER_KEYS_DIR", raising=False)
