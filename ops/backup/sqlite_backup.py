"""Consistent SQLite copies with the online backup API (runs in a throwaway python:3.12-slim container that
mounts the live volumes), plus the checks the restore drill compares against.

  python sqlite_backup.py backup OUT_DIR NAME=SRC [NAME=SRC ...]   # SRC may be a glob; writes OUT_DIR/NAME.db
                                                                   # and OUT_DIR/sqlite-manifest.json
  python sqlite_backup.py check DB [DB ...]                        # integrity + row counts as JSON
  python sqlite_backup.py adhoc DATA_DIR ARCHIVE_DIR               # archive old ad-hoc personalos*.db copies

The copy is a snapshot: the backup API copies every page in one step while the app keeps writing (WAL).
"""

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

BIG = 300 * 1024 * 1024  # above this, quick_check instead of the full integrity_check (kb.sqlite is ~1 GB)


def check(path: str) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        pragma = "quick_check" if os.path.getsize(path) > BIG else "integrity_check"
        result = [r[0] for r in db.execute(f"PRAGMA {pragma}")]
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        for t in tables:
            try:
                counts[t] = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.DatabaseError as e:  # a virtual table whose module is missing here (e.g. fts5 variants)
                counts[t] = f"error: {e}"
        return {"check": pragma, "ok": result == ["ok"], "result": result[:5], "tables": counts}
    finally:
        db.close()


def backup(out_dir: str, specs: list[str]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    manifest = {}
    for spec in specs:
        name, _, pattern = spec.partition("=")
        sources = sorted(glob.glob(pattern)) if any(c in pattern for c in "*?[") else [pattern]
        for src in sources:
            if not os.path.exists(src):
                print(f"missing {src}", file=sys.stderr)
                manifest[name] = {"source": src, "error": "missing"}
                continue
            label = name if len(sources) == 1 and "*" not in pattern else f"{name}/{os.path.basename(src).rsplit('.', 1)[0]}"
            dest = os.path.join(out_dir, label + ".db")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            started = time.time()
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
            d = sqlite3.connect(dest)
            with d:
                s.backup(d)
            s.close()
            d.execute("PRAGMA journal_mode=DELETE")  # a standalone file, no -wal needed to open it
            d.close()
            info = check(dest)
            info.update(source=src, bytes=os.path.getsize(dest), seconds=round(time.time() - started, 1))
            manifest[label] = info
            print(f"{label}: {info['bytes']} bytes, {info['check']} {'ok' if info['ok'] else info['result']}, "
                  f"{sum(v for v in info['tables'].values() if isinstance(v, int))} rows", file=sys.stderr)
    with open(os.path.join(out_dir, "sqlite-manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    if any(not v.get("ok") for v in manifest.values()):
        sys.exit("a copied database failed its check")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def adhoc(data_dir: str, archive_dir: str, keep_newest: int = 3, keep_hours: float = 48) -> None:
    """Ad-hoc copies (`personalos.<stamp>.before-x.db`, `personalos.db.bak-…`) next to the live database:
    keep the newest `keep_newest` and anything younger than `keep_hours`; the rest is copied into the backup
    store, verified by SHA-256, and only then removed from the data volume."""
    live = {"personalos.db", "personalos.db-wal", "personalos.db-shm", "personalos.db-journal"}
    copies = [p for p in glob.glob(os.path.join(data_dir, "personalos*.db*"))
              if os.path.basename(p) not in live and os.path.isfile(p)
              and not p.endswith(("-wal", "-shm", "-journal"))]
    copies.sort(key=os.path.getmtime, reverse=True)
    now = time.time()
    keep = set(copies[:keep_newest]) | {p for p in copies if now - os.path.getmtime(p) < keep_hours * 3600}
    moved = []
    os.makedirs(archive_dir, exist_ok=True)
    for p in copies:
        if p in keep:
            continue
        dest = os.path.join(archive_dir, os.path.basename(p))
        shutil.copy2(p, dest)
        if sha256(p) != sha256(dest):
            sys.exit(f"archive copy of {p} differs; nothing removed")
        for side in ("-wal", "-shm"):
            if os.path.exists(p + side):
                shutil.copy2(p + side, dest + side)
        os.remove(p)
        for side in ("-wal", "-shm"):
            if os.path.exists(p + side):
                os.remove(p + side)
        moved.append(os.path.basename(p))
    print(json.dumps({"kept": sorted(os.path.basename(p) for p in keep), "archived": moved}))


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "backup":
        backup(args[0], args[1:])
    elif cmd == "check":
        out = {p: check(p) for p in args}
        print(json.dumps(out, indent=1, sort_keys=True))
        sys.exit(0 if all(v["ok"] for v in out.values()) else 1)
    elif cmd == "adhoc":
        adhoc(args[0], args[1])
    else:
        sys.exit(__doc__)
