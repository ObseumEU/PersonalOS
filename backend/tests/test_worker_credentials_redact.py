"""The worker credential runner (pos_worker.credentials) redacts a secret even when the
subprocess output splits it across read chunks -- at a raw byte boundary (a multibyte UTF-8
character straddling the 4096-byte read) and at a character boundary. Regression for the audit
leak where each 4096-byte chunk was decoded independently, corrupting the boundary character and
leaving the secret readable."""

import os
import sys
from pathlib import Path

import pytest

# The worker package lives beside the backend (repo_root/worker); it is not on the backend's
# test path. Add it only long enough to import the credential runner, then restore sys.path and
# purge the cached modules -- so this file's import does not make `pos_worker` importable for the
# rest of the session (other suites' `importorskip("pos_worker")` must stay skipped, as they are
# without an editable worker install).
_WORKER = Path(__file__).resolve().parents[2] / "worker"
_saved_path = list(sys.path)
if _WORKER.is_dir():
    sys.path.insert(0, str(_WORKER))
try:
    credentials = pytest.importorskip("pos_worker.credentials")
finally:
    sys.path[:] = _saved_path
    for _m in [k for k in list(sys.modules) if k == "pos_worker" or k.startswith("pos_worker.")]:
        del sys.modules[_m]

Runner = credentials.Runner

SECRET = "žluťoučký-kůň-heslo-123456"  # non-ASCII, multibyte in UTF-8


def _post(path, body, headers):
    if path.endswith("check-command"):
        return 200, {"outcome": "allow"}
    return 200, {"credentials": {"pw": {"value": SECRET, "env_var": "PW"}}}


def _env():
    return {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PW": "placeholder"}


def test_secret_split_across_a_raw_byte_read_boundary_is_redacted():
    # 4095 ASCII bytes then the secret from PW: a multibyte char of the secret straddles the 4096
    # byte read, so a per-chunk decode used to mangle it. An incremental decoder keeps it whole.
    py = sys.executable
    cmd = (f'"{py}" -c "import os,sys; sys.stdout.buffer.write(b\'x\'*4095 + os.environ[\'PW\'].encode()); '
           'sys.stdout.flush()" {{cred:pw}}')
    out = Runner(post=_post, env=_env()).run(cmd)
    assert out["ok"], out
    assert SECRET not in out["output"]
    # No readable run of the secret survives past the boundary either.
    assert "luťoučký-kůň-heslo-123456" not in out["output"]
    assert "[REDACTED:pw]" in out["output"]


def test_secret_split_across_character_chunks_is_redacted():
    # Print the secret one byte at a time with flushes: it arrives across many reads.
    py = sys.executable
    cmd = (f'"{py}" -c "import os,sys,time; '
           "[ (sys.stdout.buffer.write(bytes([b])), sys.stdout.buffer.flush()) for b in os.environ['PW'].encode() ]"
           '" {{cred:pw}}')
    out = Runner(post=_post, env=_env()).run(cmd)
    assert out["ok"], out
    assert SECRET not in out["output"] and "[REDACTED:pw]" in out["output"]
