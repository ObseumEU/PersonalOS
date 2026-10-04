"""Running scenarios: the mock engine (a scripted transcript, deterministic), scoring, reports."""

import copy
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import checks, fixtures
from .checks import Transcript

REPO = Path(__file__).resolve().parents[4]
AGENTS = REPO / "agents"
WORKER = REPO / "worker"
BUILTIN = {"Edit", "Write", "Bash", "Read", "Glob", "Grep", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch"}


@dataclass
class Report:
    scenario: str
    role: str
    engine: str
    results: list = field(default_factory=list)  # [{"name", "ok", "why"}]
    cost_usd: float | None = None
    turns: int | None = None
    tool_calls: int = 0
    error: str = ""
    artifacts: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r["ok"])

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def failures(self) -> list[dict]:
        return [r for r in self.results if not r["ok"]]

    def as_dict(self) -> dict:
        return {**asdict(self), "passed": self.passed, "total": self.total,
                "score": round(self.passed / self.total, 3) if self.total else 0.0}


def agent_name(slug: str) -> str:
    try:
        return json.loads((AGENTS / slug / "agent.json").read_text(encoding="utf-8"))["name"]
    except (OSError, ValueError, KeyError):
        return slug


def rm_tree(path: str | Path) -> None:
    """Remove a temp tree (git makes its objects read-only, which Windows refuses to delete)."""
    def unlock(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=unlock)


# ------------------------------------------------------------------ transcripts


def _find(calls: list[dict], name: str, pattern: str | None) -> list[dict]:
    return [c for c in calls if c["name"] == name
            and (pattern is None or re.search(pattern, json.dumps(c["args"], ensure_ascii=False)))]


def mutate(good: dict, ops: list) -> dict:
    """A bad variant of a good transcript: ("drop", tool[, pattern]), ("set", tool, key, value[, pattern])
    on the first matching call, ("add", call), ("final", text)."""
    calls = copy.deepcopy(good["calls"])
    final = good.get("final", "")
    for op in ops:
        kind = op[0]
        if kind == "drop":
            gone = _find(calls, op[1], op[2] if len(op) > 2 else None)
            if not gone:
                raise ValueError(f"drop: no call {op[1]}")
            calls = [c for c in calls if not any(c is g for g in gone)]
        elif kind == "set":
            found = _find(calls, op[1], op[4] if len(op) > 4 else None)
            if not found:
                raise ValueError(f"set: no call {op[1]}")
            found[0]["args"][op[2]] = copy.deepcopy(op[3])
        elif kind == "add":
            calls.append(copy.deepcopy(op[1]))
        elif kind == "final":
            final = op[1]
        else:
            raise ValueError(f"unknown op {kind}")
    return {"calls": calls, "final": final}


def _subst(value, head: str):
    if isinstance(value, str):
        return value.replace("{HEAD}", head)
    if isinstance(value, dict):
        return {k: _subst(v, head) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, head) for v in value]
    return value


def run_mock(sc: dict, script: dict, root: str) -> Transcript:
    """Replay a scripted transcript: built-in calls really edit and run in the fixture folder; pos calls
    are only recorded (the mock server's job in a live run)."""
    workdir, base, sha = fixtures.make(sc.get("fixture"), root)
    name = agent_name(sc["role"])
    email = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "@agents.obseum.cz"
    env = {"GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_EMAIL": email}
    calls = []
    for c in script["calls"]:
        c = copy.deepcopy(c)
        if c["name"] in BUILTIN:
            fixtures.apply_builtin(workdir, c, env)
        elif sc.get("fixture"):
            c["args"] = _subst(c["args"], fixtures.head(workdir))
        calls.append(c)
    return Transcript(calls=calls, final=script.get("final", ""), workdir=workdir, base_commits=base, base_sha=sha)


def score(t: Transcript, sc: dict, engine: str) -> Report:
    rep = Report(sc["id"], sc["role"], engine, cost_usd=t.cost_usd, turns=t.turns, error=t.error,
                 tool_calls=len([c for c in t.calls if c["name"] != "ToolSearch"]))
    rep.results = [{"name": r.name, "ok": r.ok, "why": r.why} for r in checks.run_checks(t, sc)]
    return rep


def mock_scenario(sc: dict, script: dict | None = None) -> Report:
    root = tempfile.mkdtemp(prefix="pos-eval-")
    try:
        return score(run_mock(sc, script or sc["good"], root), sc, "mock")
    finally:
        rm_tree(root)


def selftest(sc: dict) -> list[dict]:
    """Every bad variant must fail the check it is written to break: [{"breaks", "ok", "why"}]."""
    out = []
    for b in sc.get("bad", []):
        rep = mock_scenario(sc, mutate(sc["good"], b["ops"]))
        hit = next((r for r in rep.results if r["name"] == b["breaks"]), None)
        out.append({"breaks": b["breaks"], "ok": bool(hit and not hit["ok"]),
                    "why": hit["why"] if hit and not hit["ok"] else "the check still passes" if hit else "no such check"})
    return out


# ------------------------------------------------------------------ output


def table(reports: list[Report], live: bool) -> str:
    head = ["role", "scenario", "checks", "cost $", "turns", "tool calls"] if live else ["role", "scenario", "checks"]
    rows = []
    for r in reports:
        row = [r.role, r.scenario, f"{r.passed}/{r.total}"]
        if live:
            row += [f"{r.cost_usd:.3f}" if r.cost_usd is not None else "-", str(r.turns if r.turns is not None else "-"),
                    str(r.tool_calls)]
        rows.append(row)
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows, strict=True)]
    line = lambda cells: " | ".join(str(c).ljust(w) for c, w in zip(cells, widths, strict=True))  # noqa: E731
    out = [line(head), "-+-".join("-" * w for w in widths), *(line(r) for r in rows)]
    by_role: dict[str, list[int]] = {}
    for r in reports:
        by_role.setdefault(r.role, [0, 0])
        by_role[r.role][0] += r.passed
        by_role[r.role][1] += r.total
    out.append("")
    out.append("per role: " + ", ".join(f"{k} {p}/{t}" for k, (p, t) in by_role.items()))
    if live:
        out.append(f"total cost: ${sum(r.cost_usd or 0 for r in reports):.3f}")
    fails = [(r, f) for r in reports for f in r.failures]
    if fails or any(r.error for r in reports):
        out.append("")
        out.append("failures:")
        for r in reports:
            if r.error:
                out.append(f"- {r.scenario}: run error: {r.error[:200]}")
        out += [f"- {r.scenario} / {f['name']}: {f['why']}" for r, f in fails]
    return "\n".join(out)
