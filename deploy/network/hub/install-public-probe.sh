#!/bin/sh
# Run from the repo root on the workstation: sh deploy/network/hub/install-public-probe.sh
# Copies the probe to the hub and enables the timer. The HA webhook id is copied from .186's obs
# .env straight into /etc/default/public-probe (mode 600) without being printed.
set -e
d=$(dirname "$0")
scp -q "$d/public-probe.sh" Trading:/usr/local/sbin/public-probe.sh
scp -q "$d/public-probe.service" "$d/public-probe.timer" Trading:/etc/systemd/system/
ssh AGENT 'grep "^HA_WEBHOOK_ID=" /opt/observability/platform/.env' \
  | ssh Trading 'umask 077; cat > /etc/default/public-probe'
ssh Trading 'chmod 755 /usr/local/sbin/public-probe.sh && systemctl daemon-reload && systemctl enable --now public-probe.timer && systemctl list-timers public-probe.timer --no-pager'
