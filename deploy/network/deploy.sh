#!/usr/bin/env bash
# Push the split-horizon DNS config to both resolvers. Run from the repo root:
#   bash deploy/network/deploy.sh
# Needs the ssh aliases svr03 (drosko@192.168.1.108) and Trading (root@23.88.52.230, the VPN hub).
set -euo pipefail
cd "$(dirname "$0")"
ts=$(date +%Y%m%d%H%M%S)

# svr03: LAN resolver container (lan-dns, host network, 192.168.1.108:53).
ssh svr03 "mkdir -p /opt/server/network-dns && cd /opt/server/network-dns && mkdir -p bak && cp -a *.conf bak/ 2>/dev/null || true"
scp -q svr03-dns/Dockerfile svr03-dns/dnsmasq.conf svr03-dns/docker-compose.yml dns/obseum-internal.conf svr03:/opt/server/network-dns/
ssh svr03 "cd /opt/server/network-dns && docker compose up -d --build && docker restart lan-dns >/dev/null"

# VPN hub: dnsmasq on 10.66.66.1 (the DNS every WireGuard client uses).
ssh Trading "cp -a /etc/dnsmasq.d /root/dnsmasq.d.bak-$ts"
scp -q hub/dnsmasq-vpn.conf Trading:/etc/dnsmasq.d/nexus-vpn.conf
scp -q dns/obseum-internal.conf Trading:/etc/dnsmasq.d/obseum-internal.conf
ssh Trading "dnsmasq --test && systemctl restart dnsmasq"

for n in personalos.obseum.cz smart.obseum.cloud google.com; do
  echo "$n  lan=$(ssh svr03 dig +short @192.168.1.108 $n | tail -1)  vpn=$(ssh Trading dig +short @10.66.66.1 $n | tail -1)"
done
