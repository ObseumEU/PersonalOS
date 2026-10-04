#!/bin/sh
# Install (or update) the runbook executor on svr03 as drosko (no sudo needed; linger is on).
# Run from the deployed checkout:  sh /opt/server/personalos/app/ops/runbook/install.sh
# It freezes a copy of the catalogue (backend/src/pos/ops_runbook.py) next to the executor:
# a later commit changes what the executor accepts only when this is run again.
set -eu
REPO="${REPO:-/opt/server/personalos/app}"
LIB="$HOME/.local/lib/pos-ops-runbook"
UNITS="$HOME/.config/systemd/user"
RUN_DIR="$REPO/ops/runbook/run"  # in the checkout (tracked, so git creates it as drosko)

[ "$(id -u)" != 0 ] || { echo "run as drosko, not root"; exit 1; }
id -nG | grep -qw docker || { echo "$(id -un) is not in the docker group"; exit 1; }

install -d -m 0755 "$LIB" "$UNITS"
install -m 0644 "$REPO/backend/src/pos/ops_runbook.py" "$LIB/catalogue.py"
install -m 0755 "$REPO/ops/runbook/executor.py" "$LIB/executor.py"
install -m 0644 "$REPO/ops/runbook/pos-ops-runbook.service" "$UNITS/pos-ops-runbook.service"
# The socket's directory, bind-mounted into the API container (tracked in git: it always exists).
[ -d "$RUN_DIR" ] && [ -w "$RUN_DIR" ] || { echo "$RUN_DIR missing or not writable"; exit 1; }
( cd "$LIB" && python3 -c "import catalogue; catalogue.known_hosts_text(); print(len(catalogue.ACTIONS), 'actions')" )

systemctl --user daemon-reload
systemctl --user enable pos-ops-runbook.service
systemctl --user restart pos-ops-runbook.service
sleep 1
systemctl --user --no-pager --lines=5 status pos-ops-runbook.service
[ -S "$RUN_DIR/runbook.sock" ] && echo "socket ready: $RUN_DIR/runbook.sock"
