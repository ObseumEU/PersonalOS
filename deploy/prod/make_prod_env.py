"""Write the server's .env from this PC's .env, without printing any value.

    python deploy/prod/make_prod_env.py <migration folder> [--password-file <path>]

Keeps every key the data depends on (agent keys match hashes in the database,
the MCP token, the webhook secret, the KB agent keys) and adds what the server
needs: a login password (new, written only to --password-file for the owner),
a new session secret, the compose project name, the checkout path, the Codex
login folder, and the knowlage/Nexus addresses on the proxy network.
CLAUDE_CODE_OAUTH_TOKEN stays empty here; on the server it is copied from the
knowlage .env (one `claude setup-token` for all three apps).
"""

import argparse
import secrets
from pathlib import Path

SERVER = {
    "COMPOSE_PROJECT_NAME": "personalos",
    "POS_CHECKOUT": "/opt/server/personalos/app",
    "POS_DEV_WORK": "/opt/server/personalos/dev-work",
    "CODEX_HOME_HOST": "/opt/server/personalos/codex-home",
    "POS_LOOPBACK_PORT": "8096",
    "POS_KNOWLAGE_URL": "http://knowlage:8080",
    "POS_KNOWLAGE_A2A_URL": "http://knowlage:8080/a2a/default",
    "POS_AGENT_RUNTIME": "auto",
    "POS_ENGINE_ORDER": "codex,claude",
    "POS_CLAUDE_MODEL": "claude-opus-5-5",
}
DROP = {"POS_PORT"}  # the server publishes only the loopback port


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--password-file", type=Path,
                    default=Path.home() / "Documents" / "PersonalOS-server-login.txt")
    a = ap.parse_args()
    here = Path(__file__).resolve().parents[2] / ".env"
    local = {}
    for line in here.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            local[k.strip()] = v
    env = {k: v for k, v in local.items() if k not in DROP}
    env.update(SERVER)
    env["POS_SESSION_SECRET"] = secrets.token_urlsafe(48)
    password = secrets.token_urlsafe(18)
    env["POS_PASSWORD"] = password
    env.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "")
    env.setdefault("POS_NEXUS_A2A_URL", "")
    out = a.folder / "env.prod"
    out.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    a.password_file.parent.mkdir(parents=True, exist_ok=True)
    a.password_file.write_text(
        "PersonalOS on the server: https://personalos.obseum.cz (LAN and VPN only)\n"
        f"Login password: {password}\n", encoding="utf-8")
    print(f"wrote {out.name} ({len(env)} keys) and the login password to {a.password_file}")


if __name__ == "__main__":
    main()
