"""Runs the script on a throwaway repository."""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from summarize_diff import summarize  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *args], check=True, capture_output=True)


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("keep\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "b.txt").unlink()
    (repo / "c.txt").write_text("new\n")
    out = summarize(str(repo))
    assert "a.txt" in out and "1 insertion" in out, out
    assert "  M a.txt" in out and "  D b.txt" in out and "  ? c.txt" in out, out
print("ok")
