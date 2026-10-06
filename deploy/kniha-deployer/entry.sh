#!/bin/sh
apk add --no-cache -q git openssh-client rsync curl >/dev/null
mkdir -p /root/.ssh
cat > /root/.ssh/config <<'CFG'
Host github-kniha
  HostName github.com
  User git
  IdentityFile /deployer/keys/id_kniha
  IdentitiesOnly yes
  UserKnownHostsFile /deployer/known_hosts
Host github-web
  HostName github.com
  User git
  IdentityFile /deployer/keys/id_web
  IdentitiesOnly yes
  UserKnownHostsFile /deployer/known_hosts
CFG
# GitHub host keys are pinned in /deployer/known_hosts (verified); a keyscan at boot raced the network after a power cut
cp /deployer/known_hosts /root/.ssh/known_hosts
git config --global --add safe.directory '*'
git config --global protocol.file.allow always
while true; do
  sh /deployer/sync.sh >> /deployer/state/sync.log 2>&1
  tail -n 2000 /deployer/state/sync.log > /deployer/state/sync.log.tmp && mv /deployer/state/sync.log.tmp /deployer/state/sync.log
  sleep "${INTERVAL:-60}"
done
