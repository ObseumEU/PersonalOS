"""Command line for the HR agent.

    python -m pos.hr review [--dry-run]   daily review: archive, promote, file tasks
    python -m pos.hr weekly               weekly overview as a task for the owner
    python -m pos.hr status               roster with scores (changes nothing)

Cron until the scheduler exists (spec 4.1 and task 11):
    30 6 * * *  cd /opt/personalos/backend && python -m pos.hr review
    0 7 * * 1   cd /opt/personalos/backend && python -m pos.hr weekly
"""

import argparse
import json
import sys

from .. import actors
from ..config import get_settings
from ..db import connect, migrate
from . import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pos.hr")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("weekly")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    conn = connect(get_settings().db_path)
    try:
        migrate(conn)
        actors.ensure_builtin(conn)
        if args.cmd == "review":
            out = service.daily_review(conn, apply=not args.dry_run)
        elif args.cmd == "weekly":
            out = service.weekly_report(conn)
        else:
            out = service.agents_overview(conn)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
