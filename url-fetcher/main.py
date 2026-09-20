"""PRODUCTION entrypoint. The only entrypoint a container image or a manifest can reach.

THE STRUCTURAL PROPERTY THIS FILE EXISTS FOR: there is no way to ask it for a permissive
guard. It reads three environment variables — none of them reaches the loopback allowance —
and it defines no command-line flags at all, so there is nothing for a manifest's `args:` to
set either. `Guard.allow_loopback_origin` defaults to False and is not named anywhere in this
file except in the tripwire below, which refuses to start if it is ever true. A permissive
instance is constructible only in-process, by code that passes the argument, which is what
url-fetcher/devserver.py does and what nothing shipped in the image does.

CONFIGURATION, all of it: URL_FETCHER_ALLOWED_HOSTS is the host policy and is meant to be
widened by editing the deployment; URL_FETCHER_BIND and URL_FETCHER_PORT are where it
listens. Widening the allow-list cannot relax an address class — the two checks are
conjunctive, so adding 127.0.0.1 to the list still gets refused by the address guard.
"""

import ipaddress
import os
import sys

from fetch import Fetcher
from guard import (
    ADDRESS_POLICY_NAME,
    DEFAULT_ALLOWED_HOSTS,
    Guard,
    Refused,
    address_class,
)
from server import serve

ALLOWED_HOSTS_ENV = "URL_FETCHER_ALLOWED_HOSTS"

# the three the epic is written against: the loopback origin the dev instance serves, the
# cloud metadata endpoint, and an in-cluster RFC1918 address
TRIPWIRE_LITERALS = ("127.0.0.1", "169.254.169.254", "10.0.0.1")


def allowed_hosts():
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "").strip()
    if not raw:
        return DEFAULT_ALLOWED_HOSTS
    return tuple(h.strip() for h in raw.replace("\n", ",").split(",") if h.strip())


def assert_deployed_configuration(guard):
    """A tripwire, not a test: the pod starts only if the ADDRESS-CLASS check is the thing
    refusing the three literals, and refuses to start otherwise.

    Each probe is deliberately built so that no OTHER policy can answer it. The allow-list
    CONTAINS the literal, so the host policy cannot; the URL is https, so the scheme gate
    cannot; the port is the default 443, so the port policy cannot. That leaves the address
    class, and the refusal is matched against its name rather than merely being a refusal —
    a check that asks only "was something refused" passes with the address class deleted,
    which is exactly the check this tripwire is for.

    It raises rather than asserting, because `assert` is stripped by PYTHONOPTIMIZE and that
    is an environment variable a manifest can set — the class of thing this file forecloses.
    """
    if guard.allow_loopback_origin:
        raise SystemExit("url-fetcher: the production entrypoint constructed a guard that "
                         "permits loopback; that construction exists for tests only")
    for literal in TRIPWIRE_LITERALS:
        if address_class(ipaddress.ip_address(literal)) is None:
            raise SystemExit(f"url-fetcher: address_class no longer refuses {literal}")
        url = f"https://{literal}/x"
        probe = Guard(allowed_hosts=(literal,), allowed_ports=(443,))
        try:
            probe.check(url)
        except Refused as exc:
            if ADDRESS_POLICY_NAME not in exc.message:
                raise SystemExit(
                    f"url-fetcher: {url} was refused by something other than the "
                    f"address-class policy ({exc.message}); the address-class check is "
                    "what this deployment is trusted for") from None
            continue
        raise SystemExit(f"url-fetcher: the deployed guard accepted {url}")
    # and the guard actually being served refuses them too, by whichever policy answers first
    for literal in TRIPWIRE_LITERALS:
        try:
            guard.check(f"https://{literal}/x")
        except Refused:
            continue
        raise SystemExit(f"url-fetcher: the deployed guard accepted https://{literal}/x")


def main():
    guard = Guard(allowed_hosts())
    assert_deployed_configuration(guard)
    return serve(Fetcher(guard),
                 host=os.environ.get("URL_FETCHER_BIND", "0.0.0.0"),
                 port=int(os.environ.get("URL_FETCHER_PORT", "8090")))


if __name__ == "__main__":
    sys.exit(main())
