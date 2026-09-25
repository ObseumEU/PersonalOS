"""Summarize the working tree's changes against a base (default HEAD).

    python summarize_diff.py [BASE] [--repo PATH]

Prints `git diff --stat` and then the changed files with their status
(A added, M modified, D deleted, R renamed). Untracked files are listed too.
Read-only: it never changes the repository.
"""

import argparse
import subprocess
import sys


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def summarize(repo: str = ".", base: str = "HEAD") -> str:
    stat = git(repo, "diff", "--stat", base).rstrip()
    names = [line.split("\t") for line in git(repo, "diff", "--name-status", base).splitlines() if line]
    untracked = [p for p in git(repo, "ls-files", "--others", "--exclude-standard").splitlines() if p]
    out = [f"Changes against {base}:", stat or "(no changes to tracked files)", "", "Files:"]
    out += [f"  {parts[0][0]} {' -> '.join(parts[1:])}" for parts in names]
    out += [f"  ? {p}" for p in untracked]
    if not names and not untracked:
        out.append("  (none)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", nargs="?", default="HEAD")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    try:
        print(summarize(a.repo, a.base))
    except subprocess.CalledProcessError as e:
        print(f"git failed: {e.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
