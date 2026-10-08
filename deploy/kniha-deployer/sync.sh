#!/bin/sh
# One pass of the Kniha deployer (svr03, container kniha-deployer, every 60 s):
#   1. fetch the Kniha repo and its web submodule in the team's shared workspace /work/kniha;
#   2. push main + agent/* (Kniha) and production + agent/* (web), never --force;
#   3. deploy the web (rodinne-pribehy.obseum.cz) when web origin/production moved;
#   4. deploy the app (kniha-test.obseum.cz) when Kniha origin/main moved and app/ or produkt/ changed:
#      build, health check over HTTPS, rollback to the previous image and source on failure;
#   5. write /work/kniha/.deploy/status.txt with the CURRENT state only (a successful pass clears
#      an old error: T-873 was a false alarm from a stale log tail).
# Transient network errors (DNS "Could not resolve hostname", connect refused/timed out, reset) are
# retried within the pass (3 tries, 10 s and 30 s apart) before they count as a problem: a DNS blip at
# 01:45 on 2026-10-08 stood in status.txt as a failure until the next pass.
# Installed copy: /opt/server/kniha-deployer/sync.sh (source: PersonalOS deploy/kniha-deployer/).
set -u
W=/work/kniha
S=/deployer/state
SITE=/opt/server/rodinne-pribehy
APP=/opt/server/kniha-test
APP_URL=${APP_URL:-https://kniha-test.obseum.cz}
OUT=$W/.deploy
mkdir -p "$S"
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ERR="$S/pass.err"   # problems of THIS pass only (status.txt shows them; the next good pass clears them)
: > "$ERR"
problem() { echo "$*" | tee -a "$ERR"; }

if [ ! -d "$W/.git" ]; then
  echo "$(now) cloning the workspace"
  git clone -q git@github-kniha:ObseumEU/Kniha.git "$W" || exit 1
  git -C "$W" config submodule.web.url git@github-web:roskodav/web-builder-studio.git
  git -C "$W" config submodule.Knowlage.update none
  git -C "$W" submodule update --init web || exit 1
  git -C "$W/web" fetch -q origin
  git -C "$W/web" checkout -q -B production origin/production
  for r in "$W" "$W/web"; do printf '.deploy/\n' >> "$(git -C "$r" rev-parse --absolute-git-dir)/info/exclude"; done
  git -C "$W" config user.name "Kniha Developer"; git -C "$W" config user.email "kniha@agents.obseum.cz"
  git -C "$W/web" config user.name "Kniha Developer"; git -C "$W/web" config user.email "kniha@agents.obseum.cz"
fi
mkdir -p "$OUT"

TRANSIENT='Could not resolve hostname|Temporary failure in name resolution|Name or service not known|Try again|Connection timed out|Connection refused|Connection reset|Network is unreachable|No route to host|kex_exchange_identification|Connection closed by remote host|ssh_exchange_identification'
net() {  # label cmd...: run a network git command; transient failures are retried, the rest are problems
  label=$1; shift
  for wait in 10 30 0; do
    out=$("$@" 2>&1); rc=$?
    if [ $rc = 0 ] || ! printf '%s' "$out" | grep -q -E "$TRANSIENT"; then break; fi
    [ $wait = 0 ] && break
    echo "$(now) $label: transient network error, retrying in ${wait} s" >> "$S/sync.log"  # not a problem yet
    sleep $wait
  done
  [ -n "$out" ] && printf '%s\n' "$out"
  return $rc
}

# 1. Fetch, so the agents see origin without a credential of their own.
net "kniha fetch" git -C "$W" fetch -q --prune origin 2>&1 | sed "s/^/$(now) kniha fetch: /" | while read -r l; do problem "$l"; done
net "web fetch" git -C "$W/web" fetch -q --prune origin 2>&1 | sed "s/^/$(now) web fetch: /" | while read -r l; do problem "$l"; done

# 2. Push: main and agent/* (Kniha), production and agent/* (web). Never --force: a rejected
#    push (not a fast-forward) is logged and shown in status.txt while it lasts.
push() {  # repo refspecs...
  r=$1; shift
  net "push $(basename "$r")" git -C "$r" push -q --porcelain origin "$@" 2>&1 | grep -v -E '^(To |Done|=|remote:|[ *+-]	)' | grep -v '^$' \
    | sed "s|^|$(now) push $(basename "$r"): |" | while read -r l; do problem "$l"; done
}
KB=$(git -C "$W" for-each-ref --format='%(refname:short)' refs/heads/agent/ | tr '\n' ' ')
WB=$(git -C "$W/web" for-each-ref --format='%(refname:short)' refs/heads/agent/ | tr '\n' ' ')
push "$W" main $KB
push "$W/web" production $WB

# 3. Deploy the web when origin/production moved.
net "web fetch production" git -C "$W/web" fetch -q origin production >/dev/null 2>&1
sha=$(git -C "$W/web" rev-parse origin/production)
last=$(cat "$S/deployed" 2>/dev/null || echo none)
if [ "$sha" != "$last" ] && [ "$sha" != "$(cat "$S/failed" 2>/dev/null)" ]; then
  echo "$(now) deploy $sha (was $last)"
  B="$S/build"; rm -rf "$B" "$S/prev"; mkdir -p "$B"
  git -C "$W/web" archive "$sha" | tar x -C "$B"
  rsync -a --exclude node_modules --exclude .output "$SITE/" "$S/prev/"
  EX="--exclude .env --exclude node_modules --exclude .output --exclude .tanstack --exclude .lovable"
  rsync -a --delete $EX "$B/" "$SITE/"
  if (cd "$SITE" && docker compose up -d --build) > "$OUT/last-deploy.log" 2>&1; then
    ok=0
    for i in $(seq 1 30); do
      sleep 5
      h=$(docker inspect -f '{{.State.Health.Status}}' rodinne-pribehy-web 2>/dev/null)
      if [ "$h" = healthy ] && curl -fsS -o /dev/null "$SITE_URL/"; then ok=1; break; fi
    done
    if [ $ok = 1 ]; then
      echo "$sha" > "$S/deployed"; rm -f "$S/failed"
      result="ok"
    else
      result="deployed but not healthy after 150 s (see last-deploy.log)"; echo "$sha" > "$S/failed"
    fi
  else
    echo "$sha" > "$S/failed"
    rsync -a --delete $EX "$S/prev/" "$SITE/"
    result="build failed, the old version keeps running (see last-deploy.log)"
  fi
  echo "$(now) deploy $sha: $result"
  printf '%s web %s %s\n' "$(now)" "$sha" "$result" >> "$OUT/deploys.log"
  printf '%s %s\n' "$sha" "$result" > "$S/web-result"
fi

# 4. Deploy the app (kniha-test.obseum.cz) when origin/main moved. Source: $APP/src (git archive of
#    the deployed sha), passwords: $APP/.env (never in git), svr03 extras: $APP/compose.override.yml.
appdeploy() {  # sha
  a=$1
  N="$APP/next"; rm -rf "$N"; mkdir -p "$N"
  git -C "$W" archive "$a" | tar x -C "$N" || return 1
  DCN="docker compose -p kniha-test --env-file $APP/.env -f $N/app/docker-compose.yml -f $APP/compose.override.yml"
  DCO="docker compose -p kniha-test --env-file $APP/.env -f $APP/src/app/docker-compose.yml -f $APP/compose.override.yml"
  docker image inspect kniha-test:latest >/dev/null 2>&1 && docker tag kniha-test:latest kniha-test:rollback
  if ! $DCN build >> "$AL" 2>&1; then
    docker image inspect kniha-test:rollback >/dev/null 2>&1 && docker tag kniha-test:rollback kniha-test:latest
    aresult="build failed, the old version keeps running (see app-last-deploy.log)"; return 1
  fi
  $DCN up -d --no-build >> "$AL" 2>&1
  ok=0
  for i in $(seq 1 30); do
    sleep 5
    h=$(docker inspect -f '{{.State.Health.Status}}' kniha-test 2>/dev/null)
    if [ "$h" = healthy ] && curl -fsS -o /dev/null "$APP_URL/healthz"; then ok=1; break; fi
  done
  if [ $ok = 1 ]; then
    rm -rf "$APP/prev"; [ -d "$APP/src" ] && mv "$APP/src" "$APP/prev"; mv "$N" "$APP/src"
    aresult="ok"; return 0
  fi
  echo "--- not healthy after 150 s: rolling back" >> "$AL"; docker logs --tail 60 kniha-test >> "$AL" 2>&1
  if docker image inspect kniha-test:rollback >/dev/null 2>&1 && [ -d "$APP/src" ]; then
    docker tag kniha-test:rollback kniha-test:latest
    $DCO up -d --no-build >> "$AL" 2>&1
  fi
  aresult="not healthy after 150 s, rolled back to the previous version (see app-last-deploy.log)"; return 1
}
appsha=$(git -C "$W" rev-parse origin/main)
alast=$(cat "$S/app-deployed" 2>/dev/null || echo none)
if [ -f "$APP/.env" ] && [ "$appsha" != "$alast" ] && [ "$appsha" != "$(cat "$S/app-failed" 2>/dev/null)" ]; then
  if [ "$alast" != none ] && git -C "$W" diff --quiet "$alast" "$appsha" -- app produkt 2>/dev/null; then
    echo "$appsha" > "$S/app-deployed"   # nothing the app is built from changed: the running app is this sha
  else
    echo "$(now) app deploy $appsha (was $alast)"
    AL="$OUT/app-last-deploy.log"; echo "$(now) app deploy $appsha (was $alast)" > "$AL"
    aresult=""
    if appdeploy "$appsha"; then echo "$appsha" > "$S/app-deployed"; rm -f "$S/app-failed"
    else echo "$appsha" > "$S/app-failed"; fi
    echo "$(now) app deploy $appsha: $aresult" | tee -a "$AL"
    printf '%s app %s %s\n' "$(now)" "$appsha" "$aresult" >> "$OUT/deploys.log"
    printf '%s %s\n' "$appsha" "$aresult" > "$S/app-result"
  fi
fi

# 5. Status for the agents: the current state only.
{
  echo "updated: $(now)"
  echo "kniha origin/main: $(git -C "$W" rev-parse origin/main)"
  echo "kniha local main:  $(git -C "$W" rev-parse main)"
  echo
  echo "web (https://rodinne-pribehy.obseum.cz)"
  echo "  origin/production: $sha"
  echo "  live:              $(cat "$S/deployed" 2>/dev/null)"
  [ "$(cat "$S/failed" 2>/dev/null)" = "$sha" ] && echo "  FAILED deploy of the current production: $(cut -d' ' -f2- "$S/web-result" 2>/dev/null)"
  echo
  echo "app (https://kniha-test.obseum.cz, built from origin/main app/ + produkt/)"
  echo "  live:              $(cat "$S/app-deployed" 2>/dev/null || echo none)"
  [ -f "$S/app-result" ] && echo "  last deploy:       $(cat "$S/app-result")"
  [ "$(cat "$S/app-failed" 2>/dev/null)" = "$(git -C "$W" rev-parse origin/main)" ] \
    && echo "  FAILED: origin/main does not run; fix it with a new commit on main (logs: app-last-deploy.log)"
  ah=$(docker inspect -f '{{.State.Health.Status}}' kniha-test 2>/dev/null || echo missing)
  echo "  container:         kniha-test $ah"
  echo
  if [ -s "$ERR" ]; then
    echo "PROBLEMS in this pass ($(now)):"
    tail -n 15 "$ERR"
  else
    echo "last pass: ok (fetch, push and deploys without errors)"
  fi
} > "$OUT/status.txt.tmp" && mv "$OUT/status.txt.tmp" "$OUT/status.txt"
