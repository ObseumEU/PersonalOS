#!/bin/sh
# External uptime check of the public sites, run on the Hetzner hub (23.88.52.230) every minute by
# public-probe.timer. Public DNS for *.obseum.cz points at the hub, which DNATs :80/:443 through
# WireGuard (wg0) to svr03 (10.66.66.4). The probe uses exactly that path (--resolve to 10.66.66.4),
# so a flapping tunnel or an unreachable svr03 is seen from outside the home network.
#
# Pages the owner's phone through the same Home Assistant webhook the Grafana "svr03 down" alerts
# use (automation server_outage_phone), with a Grafana-shaped payload:
#   - firing  after 3 consecutive failing runs, or >= 5 failing runs out of the last 15 (flapping);
#   - resolved after 5 consecutive good runs.
# HA is reached through the tunnel (192.168.1.56 via svr03); if the tunnel is down the page is
# retried every run until it goes through.
#
# /etc/default/public-probe (mode 600, not in git): HA_WEBHOOK_ID=<same as .186 obs .env>
# Install: see deploy/network/hub/install-public-probe.sh. Log: journalctl -t public-probe
set -u

URLS="${PROBE_URLS:-https://tesco.obseum.cz/api/health https://chatpulse.obseum.cz/ https://audexia.obseum.cz/}"
TARGET_IP="${PROBE_TARGET_IP:-10.66.66.4}"
HA_URL_BASE="${HA_URL_BASE:-http://192.168.1.56:8123/api/webhook}"
STATE_DIR="${STATE_DIR:-/var/lib/public-probe}"
DRY_RUN="${DRY_RUN:-0}"
[ -r /etc/default/public-probe ] && . /etc/default/public-probe

mkdir -p "$STATE_DIR"
HIST="$STATE_DIR/history"      # one char per run, newest last: 1 = fail, 0 = ok
ALERTED="$STATE_DIR/alerted"   # present while a page is firing
PENDING="$STATE_DIR/pending"   # payload that still has to be delivered

log() { logger -t public-probe "$*"; [ "$DRY_RUN" = 1 ] && echo "$*"; return 0; }

failed=""
for url in $URLS; do
  host=$(echo "$url" | sed -E 's#^https?://([^/]+).*#\1#')
  code=$(curl -sk -o /dev/null -m 10 --resolve "$host:443:$TARGET_IP" -w '%{http_code}' "$url" 2>/dev/null)
  case "$code" in 2??|3??) ;; *) failed="$failed $host($code)";; esac
done

hist=$(cat "$HIST" 2>/dev/null)
if [ -n "$failed" ]; then hist="${hist}1"; log "FAIL:$failed"; else hist="${hist}0"; fi
hist=$(printf '%s' "$hist" | tail -c 15)
printf '%s' "$hist" > "$HIST"

last3=$(printf '%s' "$hist" | tail -c 3)
last5=$(printf '%s' "$hist" | tail -c 5)
fails15=$(printf '%s' "$hist" | tr -cd 1 | wc -c)

payload() { # $1 = firing|resolved, $2 = summary
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"receiver":"Home Assistant phone","status":"%s","title":"[%s] Public sites unreachable via hub tunnel","message":"%s","commonLabels":{"alertname":"Public sites via hub","page":"phone","host":"hub","app":"public-urls"},"commonAnnotations":{"summary":"%s"},"alerts":[{"status":"%s","labels":{"alertname":"Public sites via hub","page":"phone","host":"hub","app":"public-urls"},"annotations":{"summary":"%s"},"startsAt":"%s"}]}' \
    "$1" "$(echo "$1" | tr a-z A-Z)" "$2" "$2" "$1" "$2" "$ts"
}

if [ ! -e "$ALERTED" ] && { [ "$last3" = 111 ] || [ "$fails15" -ge 5 ]; }; then
  touch "$ALERTED"
  payload firing "Public sites fail from the hub (23.88.52.230 -> wg0 -> svr03): ${failed:-flapping} ($fails15/15 runs failed)" > "$PENDING"
  log "PAGE firing ($fails15/15 failed, last:$failed)"
elif [ -e "$ALERTED" ] && [ "$last5" = 00000 ]; then
  rm -f "$ALERTED"
  payload resolved "Public sites reachable again from the hub (5 good runs)" > "$PENDING"
  log "PAGE resolved"
fi

if [ -s "$PENDING" ]; then
  if [ "$DRY_RUN" = 1 ] || [ -z "${HA_WEBHOOK_ID:-}" ]; then
    log "would POST: $(cat "$PENDING")"; [ "$DRY_RUN" = 1 ] || rm -f "$PENDING"
  elif curl -s -o /dev/null -m 10 -H 'Content-Type: application/json' --data-binary @"$PENDING" \
         "$HA_URL_BASE/$HA_WEBHOOK_ID"; then
    rm -f "$PENDING"; log "page delivered"
  else
    log "page delivery failed, retry next run"
  fi
fi
exit 0
