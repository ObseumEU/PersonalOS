#!/usr/bin/env bash
# Let PersonalOS send mail from one mailbox (pos.outbound_gmail, request_outbound email.send):
# one Google consent for the narrowest scope that sends, `gmail.send` (it cannot read, list or delete mail;
# threads are read with the existing gmail.readonly token). Run it over ssh from the PC; -L forwards the
# browser's return address (http://127.0.0.1:8767) to this script:
#
#   ssh -t -L 8767:127.0.0.1:8767 svr03 /opt/server/personalos/app/deploy/prod/gmail-send-login.sh david.rosko@obseum.cz
#
# The OAuth client is the one PersonalOS already uses (POS_GOOGLE_CLIENT_ID/SECRET in .env, a "Desktop app"
# client, so the loopback address works). The refresh token goes straight into .env as
# POS_GMAIL_SEND_TOKEN_<ADDRESS>, never on screen. The account is checked: a consent given for another
# Google account than the one asked for is thrown away. The api picks the token up on its next start.
set -euo pipefail
want="${1:?usage: gmail-send-login.sh <address>}"
want="$(printf '%s' "$want" | tr '[:upper:]' '[:lower:]')"
cd "${APP_DIR:-$(dirname "$0")/../..}"
port="${GMAIL_LOGIN_PORT:-8767}"
image="$(docker inspect -f '{{.Config.Image}}' personalos-api-1)"
out="$(mktemp -d)"
chmod 700 "$out"
trap 'rm -rf "$out"' EXIT

docker run --rm -i -p "127.0.0.1:${port}:${port}" --env-file .env -e WANT="$want" -e PORT="$port" \
  -v "$out:/out" --user "$(id -u):$(id -g)" --entrypoint python "$image" - <<'PY'
import base64, hashlib, http.server, json, os, secrets, sys, urllib.parse
import httpx

cid, csecret = os.environ["POS_GOOGLE_CLIENT_ID"], os.environ["POS_GOOGLE_CLIENT_SECRET"]
want, port = os.environ["WANT"], int(os.environ["PORT"])
redirect = f"http://127.0.0.1:{port}"
verifier = secrets.token_urlsafe(64)
state = secrets.token_urlsafe(16)
url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id": cid, "redirect_uri": redirect, "response_type": "code", "access_type": "offline",
    "prompt": "consent select_account", "login_hint": want,
    "scope": "openid email https://www.googleapis.com/auth/gmail.send", "state": state,
    "code_challenge_method": "S256",
    "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()})
print(f"\nOpen this link in the browser on the PC and allow sending for {want}:\n\n{url}\n", file=sys.stderr, flush=True)
got = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] != state:
            self.send_response(400); self.end_headers(); return
        got.update(code=q.get("code", [""])[0], error=q.get("error", [""])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Hotovo, okno můžeš zavřít.".encode())

    def log_message(self, *a):
        pass


server = http.server.HTTPServer(("0.0.0.0", port), Handler)
while not got:
    server.handle_request()
if not got.get("code"):
    sys.exit(f"Google did not grant access ({got.get('error') or 'no code'}).")
r = httpx.post("https://oauth2.googleapis.com/token", data={
    "client_id": cid, "client_secret": csecret, "code": got["code"], "code_verifier": verifier,
    "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
data = r.json()
if r.status_code != 200 or not data.get("refresh_token"):
    sys.exit(f"Google refused: {data.get('error_description') or data.get('error') or r.status_code}")
scopes = set((data.get("scope") or "").split())
if "https://www.googleapis.com/auth/gmail.send" not in scopes:
    sys.exit("the consent did not include gmail.send; nothing stored")
claims = json.loads(base64.urlsafe_b64decode(data["id_token"].split(".")[1] + "=="))
email = str(claims.get("email") or "").lower()
if email != want:
    sys.exit(f"consent was given for {email or '?'}, not {want}; nothing stored")
fd = os.open("/out/token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, data["refresh_token"].encode())
os.close(fd)
print(f"gmail.send granted for {email}; scopes: {' '.join(sorted(s.rsplit('/', 1)[-1] for s in scopes))}",
      file=sys.stderr)
PY

[[ -s "$out/token" ]] || { echo "no token, nothing changed" >&2; exit 1; }
name="POS_GMAIL_SEND_TOKEN_$(printf '%s' "$want" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9\n' '_')"
tmp="$(mktemp)"
chmod 600 "$tmp"
grep -v "^${name}=" .env > "$tmp" || true
printf '%s=%s\n' "$name" "$(cat "$out/token")" >> "$tmp"
cat "$tmp" > .env
rm -f "$tmp"
echo "$name stored in .env (value not shown). The api uses it after its next start (the deploy)."
