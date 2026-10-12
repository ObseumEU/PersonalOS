"""Nightly backup: .186 keeps only the newest 7 dailies (ops/backup/backup.sh step 8, T-1056)."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "backup" / "backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("rsync") is None,
                                reason="needs bash and rsync")


def _step_186():
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("# 8. Off-host: .186")
    return text[start:text.index("# 9. Off-host", start)]


def _run(store, remote, keep="7"):
    prelude = ('log() { echo "$*"; }\nmetric() { :; }\nNICE=()\nSSH_KEY=/dev/null\n'
               f'STORE="{store}"\nREMOTE="{remote}/"\nKEEP_DAILY_186={keep}\n')
    return subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + prelude + _step_186()],
                          capture_output=True, text=True, timeout=60)


def test_the_default_keeps_7_dailies_on_186():
    assert 'KEEP_DAILY_186="${KEEP_DAILY_186:-7}"' in SCRIPT.read_text(encoding="utf-8")


def test_186_drops_dailies_past_the_newest_7_and_keeps_weekly_and_local(tmp_path):
    store, remote = tmp_path / "store", tmp_path / "remote"
    for d in ("weekly", "archive"):
        (store / d).mkdir(parents=True)
    remote.mkdir()
    stamps = [f"202610{i:02d}T023000Z" for i in range(1, 15)]
    for s in stamps:
        (store / "daily" / s).mkdir(parents=True)
        (store / "daily" / s / "f").write_text(s)
    shutil.copytree(store / "daily" / stamps[0], store / "weekly" / stamps[0])
    # .186 as the old 14-day mirror left it
    subprocess.run(["rsync", "-a", f"{store}/daily", f"{store}/weekly", f"{store}/archive", f"{remote}/"],
                   check=True)

    r = _run(store, remote)

    assert r.returncode == 0, r.stderr
    assert "copied to .186" in r.stdout
    assert sorted(p.name for p in (remote / "daily").iterdir()) == stamps[-7:]
    assert [p.name for p in (remote / "weekly").iterdir()] == [stamps[0]]
    assert len(list((store / "daily").iterdir())) == 14   # svr03's own store is untouched
