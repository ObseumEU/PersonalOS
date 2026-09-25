"""Server command line, usable when the web app is down.

    python -m pos freeze [reason]   stop every agent now (kill switch)
    python -m pos unfreeze          turn the kill switch off (acts as the owner)
    python -m pos status            show the kill switch state

Inside Docker: docker compose exec api python -m pos freeze "reason"
"""

import json
import sys

from . import actors, killswitch
from .config import get_settings
from .core import Ctx
from .db import connect, migrate


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("freeze", "unfreeze", "status"):
        print(__doc__)
        return 2
    conn = connect(get_settings().db_path)
    migrate(conn)
    actors.ensure_builtin(conn)
    # Whoever can run commands on the server is treated as the owner.
    owner = Ctx(actors.owner_id(conn), via="cli")
    if argv[0] == "freeze":
        out = killswitch.freeze(conn, owner, " ".join(argv[1:]) or "frozen from the server command line")
    elif argv[0] == "unfreeze":
        out = killswitch.unfreeze(conn, owner)
    else:
        out = killswitch.state(conn)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
