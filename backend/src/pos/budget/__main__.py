"""Command line for the budget agent.

    python -m pos.budget check            run the hourly check, print the report
    python -m pos.budget status           print the last report
    python -m pos.budget record --agent A [--task T] < exec.jsonl
                                          record a `codex exec --json` run from stdin
    python -m pos.budget gate AGENT       exit 0 if the agent may run, 3 if not

Cron until the scheduler exists (spec 4.2, hourly):
    0 * * * *  cd /opt/personalos/backend && python -m pos.budget check
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from ..config import get_settings
from ..db import connect
from . import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pos.budget")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("status")
    rec = sub.add_parser("record")
    rec.add_argument("--agent", required=True)
    rec.add_argument("--task")
    g = sub.add_parser("gate")
    g.add_argument("agent")
    args = parser.parse_args(argv)

    conn = connect(get_settings().db_path)
    try:
        if args.cmd == "check":
            print(json.dumps(asdict(service.run_check(conn)), ensure_ascii=False, indent=2))
        elif args.cmd == "status":
            print(json.dumps(service.status(conn), ensure_ascii=False, indent=2))
        elif args.cmd == "record":
            run = service.record_exec(conn, sys.stdin, agent_id=args.agent, task_id=args.task)
            print(json.dumps({"thread_id": run.thread_id, "billable_tokens": run.usage.billable}))
        elif args.cmd == "gate":
            decision = service.can_run(conn, args.agent)
            print(json.dumps(asdict(decision), ensure_ascii=False))
            return 0 if decision.allowed else 3
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
