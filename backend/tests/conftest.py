import pytest


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    """Tests never read the machine's real Codex sessions (~/.codex) and never
    call a real AI engine; tests that need one bring a fake binary."""
    # Nor the machine's configuration: no POS_* variable from the shell and no
    # .env from the working directory (pytest run from the repo root would read
    # the real one), so the suite behaves the same wherever it is started.
    import os

    from pos.config import Settings

    for name in [n for n in os.environ if n.upper().startswith("POS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # Each test starts with a clean login rate limit (pos.auth.LoginLimiter is per process).
    from pos import auth

    monkeypatch.setattr(auth, "login_limiter", auth.LoginLimiter())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_CLAUDE_DISABLED", "1")
    # Nor the real knowlage: without a key, files are not pushed and search uses file names.
    monkeypatch.delenv("POS_KNOWLAGE_API_KEY", raising=False)
    # Tests create their own agents; the role agents from agents/*/agent.json only where a test asks.
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "0")
    # Agents' default grants for everything (pos.access autonomy) are off here, so the grant
    # mechanics stay testable; test_autonomy.py turns them on.
    monkeypatch.setenv("POS_AUTONOMY", "0")
    monkeypatch.delenv("POS_WORKER_KEYS_DIR", raising=False)
    # New mail is routed at once here; test_support.py turns the customer-issue intake on.
    monkeypatch.setenv("POS_SUPPORT_INTAKE", "0")
