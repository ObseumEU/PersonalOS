"""Host backup timer with catch-up (deploy/backup, T-379)."""

import configparser
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

BACKUP = Path(__file__).resolve().parents[2] / "deploy" / "backup"
SCRIPT = BACKUP / "pos-backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("flock") is None,
                                reason="needs bash and flock")


def _units(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(BACKUP / name)
    return cp


def test_timer_catches_up_missed_runs():
    t = _units("pos-backup@.timer")["Timer"]
    assert t["OnCalendar"] == "*-*-* 10:00"
    assert t["Persistent"] == "true"
    assert t["RandomizedDelaySec"] == "5min"
    assert _units("pos-backup@.timer")["Install"]["WantedBy"] == "timers.target"
    s = _units("pos-backup@.service")["Service"]
    assert s["Type"] == "oneshot"
    assert s["ExecStart"].endswith("pos-backup.sh %i")


def _run(tmp_path, backup_age_h=None):
    env_dir, target = tmp_path / "etc", tmp_path / "backups"
    env_dir.mkdir()
    target.mkdir()
    marker = tmp_path / "ran"
    if backup_age_h is not None:
        old = target / "20260929T095442Z"
        old.mkdir()
        (old / "kb.tar").write_text("x")
        t = time.time() - backup_age_h * 3600
        os.utime(old / "kb.tar", (t, t))
        os.utime(old, (t, t))
    (env_dir / "knowlage.env").write_text(
        f'BACKUP_CMD="touch {marker}"\nBACKUP_DIR="{target}"\nMIN_AGE_H=12\n')
    env = {**os.environ, "POS_BACKUP_ENV_DIR": str(env_dir), "POS_BACKUP_LOCK_DIR": str(tmp_path)}
    r = subprocess.run(["bash", str(SCRIPT), "knowlage"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return marker.exists(), r.stdout, sorted(p.name for p in target.rglob("*"))


def test_missed_run_after_downtime_runs_backup(tmp_path):
    # svr03 case: last backup 29. 9. ~10:00, boot 30. 9. 14:41 -> about 29 h old
    ran, out, files = _run(tmp_path, backup_age_h=29)
    assert ran and "29 h old, running" in out
    assert files == ["20260929T095442Z", "kb.tar"]  # nothing deleted


def test_fresh_backup_is_skipped(tmp_path):
    ran, out, _ = _run(tmp_path, backup_age_h=1)
    assert not ran and "skipping" in out


def test_no_backup_yet_runs(tmp_path):
    ran, out, _ = _run(tmp_path)
    assert ran and "no backup" in out


def test_missing_env_file_fails(tmp_path):
    env = {**os.environ, "POS_BACKUP_ENV_DIR": str(tmp_path)}
    r = subprocess.run(["bash", str(SCRIPT), "nexus"], env=env, capture_output=True, text=True)
    assert r.returncode == 2
