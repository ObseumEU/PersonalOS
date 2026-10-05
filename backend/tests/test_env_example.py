"""Every POS_* setting the code reads is named in .env.example (names and comments only), so the
operator can find it. Settings the worker sets for its own child processes are not configuration."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "backend" / "src", ROOT / "worker", ROOT / "shared"]
pytestmark = pytest.mark.skipif(not (ROOT / ".env.example").exists(), reason="only the backend is here")

# Set by the worker or the server for a child process, never by the operator.
INTERNAL = {
    "POS_AGENT_KEY_FILE", "POS_AGENT_WORKDIR", "POS_BASH_ALLOWED", "POS_CHILD_PIDFILE",
    "POS_CRED_SESSION", "POS_RUN_ID", "POS_TASK_ID",
}
# Names built from a prefix (POS_GMAIL_TOKEN_<ADDRESS>…): the prefix is documented with a placeholder.
PREFIXES = ("POS_A2A_KEY_", "POS_BUDGET_", "POS_HR_", "POS_GMAIL_TOKEN_", "POS_GMAIL_COMPOSE_TOKEN_",
            "POS_GMAIL_SEND_TOKEN_")


def _names_in_code() -> set[str]:
    names: set[str] = set()
    for base in SOURCES:
        for f in base.rglob("*.py"):
            if "tests" in f.parts:
                continue
            names.update(re.findall(r"\bPOS_[A-Z0-9_]+", f.read_text(encoding="utf-8", errors="ignore")))
    return names


def test_every_setting_the_code_reads_is_in_env_example():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*(POS_[A-Z0-9_]+)(?:<[A-Z]+>)?=", example, re.M))
    missing = sorted(
        n for n in _names_in_code() - INTERNAL
        if n not in documented and not any(n.startswith(p) for p in PREFIXES)
    )
    assert not missing, f"add these to .env.example (name and a comment, no value): {missing}"


def test_env_example_has_no_values_for_secrets():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        m = re.match(r"^#?\s*(\w*(?:SECRET|TOKEN|KEY|PASSWORD)\w*)=(.*)$", line)
        if m and not m.group(1).endswith("_FILE"):
            assert m.group(2).strip() == "", f"{m.group(1)} has a value in .env.example"
