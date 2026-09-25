"""Command line: ``python -m pos.guard <command>``.

check-range OLD NEW [--repo PATH]   exit 1 if a commit breaks the constitution
pre-receive [--repo PATH]           read "old new ref" lines from stdin (git hook)
check-command CMD [--external]      classify a shell command for an agent
"""

import argparse
import sys
from pathlib import Path

from . import commands, gitcheck
from .rules import SimpleActor


def _report(results) -> int:
    problems = [p for r in results for p in r.problems]
    if not all(r.active for r in results):
        for r in results:
            for p in r.unsigned_protected:
                print(f"constitution (draft, not enforced yet): {p.describe()}", file=sys.stderr)
    for p in problems:
        print(f"constitution: rejected {p.describe()}", file=sys.stderr)
    if problems:
        print(
            "constitution: only the owner may change protected files (docs/CONSTITUTION.md). "
            "Propose the change as a task for the owner.",
            file=sys.stderr,
        )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pos.guard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rng = sub.add_parser("check-range")
    rng.add_argument("old")
    rng.add_argument("new")
    rng.add_argument("--repo", type=Path, default=Path("."))

    hook = sub.add_parser("pre-receive")
    hook.add_argument("--repo", type=Path, default=Path("."))

    cmd = sub.add_parser("check-command")
    cmd.add_argument("command")
    cmd.add_argument("--external", action="store_true", help="run was triggered by outside content")

    args = parser.parse_args(argv)

    if args.cmd == "check-range":
        return _report([gitcheck.check_range(args.repo, args.old, args.new)])
    if args.cmd == "pre-receive":
        results = []
        for line in sys.stdin:
            parts = line.split()
            if len(parts) == 3:
                results.append(gitcheck.check_range(args.repo, parts[0], parts[1]))
        return _report(results)
    if args.cmd == "check-command":
        trigger = commands.Trigger.EXTERNAL if args.external else commands.Trigger.MEMBER
        decision = commands.evaluate(args.command, SimpleActor("cli"), trigger)
        print(f"{decision.outcome.value} {decision.rule or ''} {decision.reason}".strip())
        return 0 if decision.allowed else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
