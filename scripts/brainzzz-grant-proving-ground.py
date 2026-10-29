#!/usr/bin/env python3
"""Measure the brainzzz jwt-bearer grant against a deployed Keycloak, end to end.

Plays brainzzz's STS: mints RFC 7523 assertions with a private key whose public half is the
realm's `brainzzz` identity provider (BRAINZZZ_STS_PUBLIC_KEY in .env.<env>, registered by
keycloak-register-brainzzz.sh), exchanges them at the realm's token endpoint as the `brainzzz`
client, and drives the resulting access token into /mcp. Everything runs over the public URLs,
so what is measured is the deployed configuration, not a local approximation of it.

Needs: the client secret and issuer from .env.<env> (--env), the private key (--key), one realm
user already linked to the provider (--sub; keycloak-link-brainzzz-users.sh) and one address on
the realm's domain that is NOT linked (--unlinked-sub). Checks:

  linked_ok           a linked sub gets 200 and a token for the /mcp audience carrying their email
  no_refresh_token    the grant issues no refresh token (transient session)
  reuse_refused       the same assertion a second time is refused (jti is single-use)
  unlinked_refused    an unlinked sub gets invalid_grant "User not found" — the provisioning gate
  foreign_domain      a linked sub outside the realm's domain (--foreign-sub, a fixture created
                      and linked on staging so that only the policy can refuse it) is refused
                      by the client policy's sub regex, not by the user lookup
  missing_act         an assertion without `act` is refused by the client policy
  wrong_aud           aud = the /mcp URL (not the realm) is refused
  stale_iat           an assertion issued 10 minutes ago is refused: the 300 s cap runs from
                      iat (a long exp is accepted and clamped, not refused)
  mcp_initialize_ok   POST /mcp initialize with the token is 200; with a bogus token 401
  provision_new       the partner's client-credentials token provisions --provision-sub (a
                      fixture address on the realm domain): 200, and the grant then works
                      for that sub; a repeat reports created=false, newly_linked=false
  provision_refused   the same call with a user token is 401, for an address outside the
                      domain 403, and for an alias not on the partner list 404

Exit 0 all PASS, 1 any FAIL, 2 could not run (missing inputs, key, or network). Refuses a host
that is not a staging one unless --allow-prod is given, because a production realm has no test
key and the run would only litter its event log.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import jwt  # PyJWT with the cryptography backend

ROOT = Path(__file__).resolve().parent.parent
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def read_env(env: str) -> dict:
    path = ROOT / f".env.{env}"
    if not path.exists():
        sys.exit(f"could not run: {path} does not exist")
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def mint(key: str, kid: str, issuer: str, aud: str, sub: str, *, ttl=60, act=True, jti=None, issued_ago=0) -> str:
    now = int(time.time()) - issued_ago
    claims = {
        "iss": issuer, "sub": sub, "email": sub, "email_verified": True,
        "preferred_username": sub, "aud": aud,
        "iat": now, "nbf": now, "exp": now + ttl, "jti": jti or str(uuid.uuid4()),
    }
    if act:
        claims["act"] = {"sub": "brainzzz", "user_id": "proving-ground"}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def post(url: str, data: dict | str, headers: dict) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data.encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def post_form(url, data: dict) -> tuple[int, dict]:
    status, text = post(url, data, {"Content-Type": "application/x-www-form-urlencoded",
                                    "Accept": "application/json"})
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, {"raw": text[:300]}


def exchange(token_url, client_id, secret, assertion) -> tuple[int, dict]:
    return post_form(token_url, {
        "grant_type": GRANT, "client_id": client_id, "client_secret": secret, "assertion": assertion,
    })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="daly-staging", help=".env.<env> holding BRAINZZZ_CLIENT_SECRET and BRAINZZZ_STS_ISSUER")
    ap.add_argument("--host", default="staging.genegenie.broadinstitute.org")
    ap.add_argument("--key", default=os.path.expanduser("~/.secrets/brainzzz-test-sts/private.pem"))
    ap.add_argument("--kid", default="proving-ground")
    ap.add_argument("--sub", required=True, help="a realm user linked to the brainzzz provider")
    ap.add_argument("--unlinked-sub", required=True, help="an address on the realm domain with no link")
    ap.add_argument("--foreign-sub", default="brainzzz-outsider@example.com",
                    help="a LINKED fixture user outside the realm domain (keycloak-link-brainzzz-users.sh --create)")
    ap.add_argument("--provision-sub", default="brainzzz-provisioned@broadinstitute.org",
                    help="a fixture address the provisioning check creates and links (idempotent)")
    ap.add_argument("--allow-prod", action="store_true")
    a = ap.parse_args()

    if "staging" not in a.host and not a.allow_prod:
        sys.exit(f"could not run: {a.host} is not a staging host (pass --allow-prod to insist)")
    env = read_env(a.env)
    secret, issuer = env.get("BRAINZZZ_CLIENT_SECRET"), env.get("BRAINZZZ_STS_ISSUER")
    if not secret or not issuer:
        sys.exit(f"could not run: BRAINZZZ_CLIENT_SECRET / BRAINZZZ_STS_ISSUER missing from .env.{a.env}")
    try:
        key = Path(a.key).read_text()
    except OSError as e:
        sys.exit(f"could not run: {e}")

    realm = f"https://{a.host}/auth/realms/genetics"
    token_url = f"{realm}/protocol/openid-connect/token"
    mcp_url = f"https://{a.host}/mcp"
    domain = a.sub.rsplit("@", 1)[-1]
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail):
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name:18s} {detail}")

    def mint_(sub=a.sub, **kw):
        return mint(key, a.kid, issuer, kw.pop("aud", realm), sub, **kw)

    try:
        first = mint_()
        status, body = exchange(token_url, "brainzzz", secret, first)
        access = body.get("access_token")
        claims = jwt.decode(access, options={"verify_signature": False}) if access else {}
        aud = claims.get("aud", [])
        aud = aud if isinstance(aud, list) else [aud]
        check("linked_ok", status == 200 and claims.get("iss") == realm and mcp_url in aud
              and claims.get("email", "").lower() == a.sub.lower(),
              f"status={status} iss_ok={claims.get('iss') == realm} aud={aud} email={claims.get('email')} "
              f"exp_in={claims.get('exp', 0) - int(time.time())}s"
              + ("" if status == 200 else f" body={body}"))
        check("no_refresh_token", status == 200 and "refresh_token" not in body,
              f"keys={sorted(body)}")

        status, body = exchange(token_url, "brainzzz", secret, first)
        check("reuse_refused", status == 400, f"status={status} {body.get('error_description', body)}")

        status, body = exchange(token_url, "brainzzz", secret, mint_(sub=a.unlinked_sub))
        check("unlinked_refused", status == 400 and body.get("error") == "invalid_grant"
              and "user not found" in body.get("error_description", "").lower(),
              f"status={status} {body.get('error')}: {body.get('error_description', body)}")

        status, body = exchange(token_url, "brainzzz", secret, mint_(sub=a.foreign_sub))
        desc = body.get("error_description", "")
        check("foreign_domain", status == 400 and "user not found" not in desc.lower(),
              f"status={status} {desc or body}" + (" (fixture not linked: lookup refused it, not the policy)"
                                                  if "user not found" in desc.lower() else ""))

        status, body = exchange(token_url, "brainzzz", secret, mint_(act=False))
        check("missing_act", status == 400, f"status={status} {body.get('error_description', body)}")

        status, body = exchange(token_url, "brainzzz", secret, mint_(aud=mcp_url))
        check("wrong_aud", status == 400, f"status={status} {body.get('error_description', body)}")

        status, body = exchange(token_url, "brainzzz", secret, mint_(ttl=660, issued_ago=600))
        check("stale_iat", status == 400, f"status={status} {body.get('error_description', body)}")

        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "brainzzz-grant-proving-ground", "version": "0"}}})
        hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        s_ok, t_ok = post(mcp_url, init, {**hdr, "Authorization": f"Bearer {access}"}) if access else (0, "")
        s_bad, _ = post(mcp_url, init, {**hdr, "Authorization": "Bearer not-a-token"})
        check("mcp_initialize_ok", s_ok == 200 and s_bad == 401,
              f"with_token={s_ok} bogus_token={s_bad} serverInfo={'serverInfo' in t_ok}")

        # the provisioning endpoint: the partner's own service account creates and links a user
        status, body = post_form(token_url, {
            "grant_type": "client_credentials", "client_id": "brainzzz", "client_secret": secret})
        sa_token = body.get("access_token")
        provision_url = f"https://{a.host}/provision/brainzzz"
        hdr_sa = {"Content-Type": "application/json", "Authorization": f"Bearer {sa_token}"}
        s1, b1 = post(provision_url, json.dumps({"email": a.provision_sub}), hdr_sa)
        s2, b2 = post(provision_url, json.dumps({"email": a.provision_sub}), hdr_sa)
        status, body = exchange(token_url, "brainzzz", secret, mint_(sub=a.provision_sub))
        try:
            r1, r2 = json.loads(b1), json.loads(b2)
        except json.JSONDecodeError:
            r1, r2 = {"raw": b1[:200]}, {"raw": b2[:200]}
        check("provision_new", sa_token is not None and s1 == 200 and s2 == 200
              and r2.get("created") is False and r2.get("newly_linked") is False and status == 200,
              f"first={s1} {r1} repeat={s2} {r2} grant_after={status}")

        hdr_user = {"Content-Type": "application/json", "Authorization": f"Bearer {access}"}
        s_user, _ = post(provision_url, json.dumps({"email": a.provision_sub}), hdr_user)
        s_out, _ = post(provision_url, json.dumps({"email": "outsider@example.com"}), hdr_sa)
        s_alias, _ = post(f"https://{a.host}/provision/nobody", json.dumps({"email": a.provision_sub}), hdr_sa)
        check("provision_refused", (s_user, s_out, s_alias) == (401, 403, 404),
              f"user_token={s_user} foreign={s_out} unknown_alias={s_alias}")
    except (urllib.error.URLError, OSError) as e:
        print(f"could not run: {e}", file=sys.stderr)
        return 2

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS against {realm} (domain {domain})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
