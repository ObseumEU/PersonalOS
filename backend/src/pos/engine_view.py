"""Which engine and model each agent runs on, for the UI (read-only).

Derived from the agent's setting, the platform defaults, the engine pauses and
the agent's last run. Runs record the engine but not the model, so a run's
model is the one that engine is configured with now (the agent's Claude model,
POS_CLAUDE_MODEL, or POS_CODEX_MODEL / the Codex CLI's own config).
"""

import os
import re
import sqlite3
from pathlib import Path

from . import engines

MODEL_NAMES = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable"}


def codex_model() -> str | None:
    """POS_CODEX_MODEL, else the top-level `model` in the Codex CLI config."""
    if m := os.environ.get("POS_CODEX_MODEL"):
        return m
    cfg = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("["):
                break
            if m := re.match(r'\s*model\s*=\s*"([^"]+)"', line):
                return m[1]
    except OSError:
        pass
    return None


def model_for(engine: str, actor_model: str | None) -> str | None:
    if engine == "claude":
        return actor_model or engines.default_model("claude")
    return codex_model()


def pretty_model(model: str | None) -> str | None:
    """claude-opus-5-5 -> Opus 5.5; other names stay as they are."""
    if not model:
        return None
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(\[1m\])?", model)
    if not m:
        return model
    family, major, minor, long = m.groups()
    return f"{MODEL_NAMES.get(family, family.title())} {major}{'.' + minor if minor else ''}{' 1M' if long else ''}"


def label(engine: str | None, model: str | None, fallback: bool = False) -> str:
    if not engine:
        return "not run yet"
    name = {"claude": "Claude", "codex": "Codex"}.get(engine, engine)
    parts = [name, pretty_model(model)] + (["fallback"] if fallback else [])
    return " · ".join(p for p in parts if p)


class Viewer:
    """One per request: the platform-wide state is read once."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.order = engines.auto_order()
        self.paused = {e: engines.paused_until(conn, e) for e in engines.ENGINES}
        self.auto_now = next((e for e in self.order if not self.paused[e]), None)

    def for_actor(self, row: sqlite3.Row) -> dict | None:
        if row["kind"] == "human":
            return None
        setting = row["engine"] or engines.default_engine()
        last = self.conn.execute(
            "SELECT engine, started_at, status FROM runs WHERE actor_id = ? AND engine IS NOT NULL ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if setting == "auto":
            now, now_fallback = self.auto_now, self.auto_now not in (None, self.order[0])
        else:
            now, now_fallback = setting, False
        view = {
            "setting": setting,
            "primary": self.order[0] if setting == "auto" else setting,
            "now": {"engine": now, "model": model_for(now, row["model"]) if now else None,
                    "fallback": now_fallback, "paused_until": self.paused.get(now) if setting != "auto" else None},
            "last_run": None,
        }
        view["now"]["label"] = label(now, view["now"]["model"], now_fallback) if now else "all engines paused"
        if last:
            fb = setting == "auto" and last["engine"] != self.order[0]
            model = model_for(last["engine"], row["model"])
            view["last_run"] = {"engine": last["engine"], "model": model, "fallback": fb, "at": last["started_at"],
                                "running": last["status"] == "running", "label": label(last["engine"], model, fb)}
        return view
