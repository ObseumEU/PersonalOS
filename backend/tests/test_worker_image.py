"""The worker image (worker/Dockerfile) brings what the agents' apps need."""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "worker" / "Dockerfile"


def test_worker_image_installs_node_22_not_debian_nodejs():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # Debian's nodejs is v20: /work/kniha/app needs >= 22.5 (node:sqlite).
    assert not re.search(r"apt-get install[^\n]*\bnodejs\b", text)
    assert int(re.search(r"ARG NODE_MAJOR=(\d+)", text).group(1)) >= 22
    minimum = tuple(int(p) for p in re.search(r"ARG NODE_MIN=([\d.]+)", text).group(1).split("."))
    assert minimum >= (22, 5, 0)
    assert "sha256sum -c" in text  # the tarball is verified
    assert "require('node:sqlite')" in text and "npx --version" in text
