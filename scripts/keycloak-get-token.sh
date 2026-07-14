#!/bin/bash
set -euo pipefail

# Obtain a GeneGenie MCP access token via the browser authorization-code + PKCE flow — the same
# flow brainzzz uses — and print it. Paste it into MCP Inspector's "Bearer Token" field, or use it
# directly against the MCP server:
#   curl -H "Authorization: Bearer <token>" https://genegenie.broadinstitute.org/mcp ...
#
# Run this ON YOUR LAPTOP (needs a browser, python3, curl, openssl). It does NOT need cluster /
# kubectl access — it talks only to the public Keycloak. When the browser opens, log in with an
# allow-listed (e.g. broadinstitute.org) account. A one-shot local web server catches the OAuth
# redirect, so you don't copy the code by hand.
#
# The REDIRECT_URI below must be registered on the client first (idempotent):
#   ./scripts/keycloak-register-client.sh authorized_agent http://localhost:8765/callback
#
# Config (env or .env):
#   CLIENT_ID       default: authorized_agent
#   CLIENT_SECRET   required (the secret keycloak-register-client.sh printed)
#   REDIRECT_URI    default: http://localhost:8765/callback (must be registered + port free)
#   OAUTH_ISSUER    default: https://genegenie.broadinstitute.org/auth/realms/genetics
#   SCOPE           default: openid email profile

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "${ROOT_DIR}/.env" ] && { set -a; . "${ROOT_DIR}/.env"; set +a; }

CLIENT_ID="${CLIENT_ID:-authorized_agent}"
REDIRECT_URI="${REDIRECT_URI:-http://localhost:8765/callback}"
OAUTH_ISSUER="${OAUTH_ISSUER:-https://genegenie.broadinstitute.org/auth/realms/genetics}"
SCOPE="${SCOPE:-openid email profile}"
: "${CLIENT_SECRET:?Set CLIENT_SECRET (the authorized_agent client secret) in .env or the environment}"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

# PKCE (S256) + CSRF state
VERIFIER="$(openssl rand -base64 60 | tr -dc 'A-Za-z0-9' | cut -c1-64)"
CHALLENGE="$(printf '%s' "$VERIFIER" | openssl dgst -sha256 -binary | openssl base64 | tr '+/' '-_' | tr -d '=\n')"
STATE="$(openssl rand -hex 8)"

AUTH_URL="$(python3 - "$OAUTH_ISSUER" "$CLIENT_ID" "$REDIRECT_URI" "$SCOPE" "$CHALLENGE" "$STATE" <<'PY'
import sys, urllib.parse
issuer, cid, redirect, scope, challenge, state = sys.argv[1:7]
q = urllib.parse.urlencode({
    "client_id": cid, "response_type": "code", "redirect_uri": redirect, "scope": scope,
    "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
})
print(f"{issuer}/protocol/openid-connect/auth?{q}")
PY
)"

echo "Opening your browser to log in (use an allow-listed account)..."
echo "If it does not open, paste this URL into your browser:"
echo "  ${AUTH_URL}"
( command -v open >/dev/null 2>&1 && open "${AUTH_URL}" ) \
  || ( command -v xdg-open >/dev/null 2>&1 && xdg-open "${AUTH_URL}" ) || true

# one-shot listener on the redirect URI to capture ?code=...
CODE="$(python3 - "$REDIRECT_URI" "$STATE" <<'PY'
import sys, urllib.parse, http.server
u = urllib.parse.urlsplit(sys.argv[1]); expected_state = sys.argv[2]
host, port = (u.hostname or "127.0.0.1"), (u.port or 80)
captured = {}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        captured["code"] = (q.get("code") or [""])[0]
        captured["state"] = (q.get("state") or [""])[0]
        captured["error"] = (q.get("error") or [""])[0]
        ok = bool(captured["code"])
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        note = "Login complete — close this tab and return to the terminal." if ok else f"Auth error: {captured['error']}"
        self.wfile.write(f"<html><body><h3>{note}</h3></body></html>".encode())
    def log_message(self, *a): pass
srv = http.server.HTTPServer((host, port), H)
srv.handle_request()  # serve exactly one request, then exit
if captured.get("error"):
    sys.stderr.write(f"authorization error: {captured['error']}\n"); sys.exit(1)
if captured.get("state") != expected_state:
    sys.stderr.write("state mismatch — aborting\n"); sys.exit(1)
print(captured.get("code", ""))
PY
)"

[ -n "${CODE}" ] || { echo "no authorization code received" >&2; exit 1; }

RESP="$(curl -s "${OAUTH_ISSUER}/protocol/openid-connect/token" \
  -d grant_type=authorization_code -d client_id="${CLIENT_ID}" -d client_secret="${CLIENT_SECRET}" \
  -d redirect_uri="${REDIRECT_URI}" -d code="${CODE}" -d code_verifier="${VERIFIER}")"

TOKEN="$(printf '%s' "${RESP}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')"
if [ -z "${TOKEN}" ]; then
  echo "token exchange failed:" >&2
  printf '%s\n' "${RESP}" | python3 -m json.tool >&2 2>/dev/null || printf '%s\n' "${RESP}" >&2
  exit 1
fi

# eyeball the claims the mcp-server checks (audience + email + expiry)
printf '%s' "${TOKEN}" | python3 -c '
import sys, base64, json
p = sys.stdin.read().split(".")[1]; p += "=" * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
print("token claims: aud=%s  email=%s  exp=%s" % (c.get("aud"), c.get("email") or c.get("preferred_username"), c.get("exp")))
'
echo
echo "ACCESS TOKEN (paste into MCP Inspector's Bearer Token field, or use with curl):"
echo "${TOKEN}"
