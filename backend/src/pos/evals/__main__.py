"""python -m pos.evals [--role R | --all] [--scenario ID] [--live] [--json] (see pos.evals)."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import runner, scenarios
from .checks import Transcript


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pos.evals", description="Agent eval suite (see pos.evals).")
    ap.add_argument("--role", help=f"one role: {', '.join(scenarios.roles())} (or ceo/hcs/kniha/se/sre/am)")
    ap.add_argument("--all", action="store_true", help="every role (the default without --role/--scenario)")
    ap.add_argument("--scenario", help="scenario id(s), comma-separated")
    ap.add_argument("--live", action="store_true", help="run the real agent with the local claude CLI (costs money)")
    ap.add_argument("--json", action="store_true", help="print the reports as JSON")
    ap.add_argument("--max-budget-usd", type=float, default=0.30, help="live: the cap per run (default 0.30)")
    ap.add_argument("--total-budget-usd", type=float, default=None, help="live: stop starting runs past this total")
    ap.add_argument("--first-per-role", action="store_true", help="only the first scenario of each role")
    ap.add_argument("--out", help="live: where to keep prompts, streams and transcripts (default: a temp folder)")
    ap.add_argument("--keep", action="store_true", help="live: keep the work folders")
    ap.add_argument("--rescore", metavar="DIR", help="score the transcripts a live run kept in DIR again (free)")
    ap.add_argument("--no-selftest", action="store_true", help="mock: skip the bad transcripts")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Czech and arrows on a Windows console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    chosen = scenarios.select(None if a.all else a.role, a.scenario)
    if a.first_per_role:
        seen: set[str] = set()
        chosen = [s for s in chosen if not (s["role"] in seen or seen.add(s["role"]))]
    reports: list[runner.Report] = []
    if a.rescore:
        for sc in chosen:
            f = Path(a.rescore) / sc["id"] / "transcript.json"
            if not f.exists():
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
            workdir = d.get("workdir") if d.get("workdir") and Path(d["workdir"]).is_dir() else None
            t = Transcript(calls=d["calls"], final=d.get("final") or "", workdir=workdir,
                           base_commits=d.get("base_commits") or 0, base_sha=d.get("base_sha") or "",
                           cost_usd=d.get("cost_usd"), turns=d.get("turns"), error=d.get("error") or "")
            reports.append(runner.score(t, sc, "live"))
        print(json.dumps([r.as_dict() for r in reports], ensure_ascii=False, indent=1) if a.json
              else runner.table(reports, live=True))
        return 0
    if not a.live:
        bad_results = {}
        for sc in chosen:
            reports.append(runner.mock_scenario(sc))
            if not a.no_selftest:
                bad_results[sc["id"]] = runner.selftest(sc)
        broken = [(sid, b) for sid, bs in bad_results.items() for b in bs if not b["ok"]]
        failed_good = [r for r in reports if r.passed != r.total]
        if a.json:
            print(json.dumps({"reports": [r.as_dict() for r in reports], "selftest": bad_results}, ensure_ascii=False,
                             indent=1))
        else:
            print(runner.table(reports, live=False))
            n = sum(len(b) for b in bad_results.values())
            if bad_results:
                print(f"\nselftest: {n - len(broken)}/{n} bad transcripts fail the check they target")
                for sid, b in broken:
                    print(f"- {sid} / {b['breaks']}: {b['why']}")
        return 1 if broken or failed_good else 0

    from . import live

    out = Path(a.out or tempfile.mkdtemp(prefix="pos-evals-live-"))
    out.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    for sc in chosen:
        if a.total_budget_usd is not None and spent + a.max_budget_usd > a.total_budget_usd + 1e-9:
            print(f"skipping {sc['id']}: the total budget (${a.total_budget_usd:.2f}) would be exceeded "
                  f"(spent ${spent:.3f})", file=sys.stderr)
            continue
        print(f"running {sc['id']} ({sc['role']}) live, cap ${a.max_budget_usd:.2f} ...", file=sys.stderr, flush=True)
        t = live.run(sc, a.max_budget_usd, out, keep=a.keep)
        try:
            rep = runner.score(t, sc, "live")
        finally:
            t.cleanup()
        rep.artifacts = str(out / sc["id"])
        spent += rep.cost_usd or 0.0
        reports.append(rep)
        print(f"  {rep.passed}/{rep.total} checks, ${rep.cost_usd or 0:.3f}, {rep.turns} turns, "
              f"{rep.tool_calls} tool calls", file=sys.stderr, flush=True)
    if a.json:
        print(json.dumps({"reports": [r.as_dict() for r in reports], "total_cost_usd": round(spent, 4),
                          "artifacts": str(out)}, ensure_ascii=False, indent=1))
    else:
        print(runner.table(reports, live=True))
        print(f"artifacts: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
