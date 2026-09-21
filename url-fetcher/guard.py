"""What may be fetched, decided before a socket is opened.

The whole security value of this service is here. Two properties are structural rather than
configured, and both are load-bearing:

1. The loopback allowance is a CONSTRUCTOR ARGUMENT. Nothing in this module reads the
   environment, argv or a config file, so no manifest can produce a permissive guard — only
   in-process code that passes `allow_loopback_origin=True` can, and the production entrypoint
   never names it. The local proving ground serves its fixture over loopback and the deployed
   fetcher must refuse exactly that, so the two are two constructions of one guard rather than
   one guard with a switch.
2. The allowance unlocks LOOPBACK AND NOTHING ELSE. The metadata endpoint, RFC1918, CGNAT,
   link-local and every other non-global class stay refused under it, which is what lets the
   proving ground measure them against the permissive instance.

The host policy is an allow-list and is configuration: the production entrypoint reads it from
the environment and hands it in. A refusal NAMES the policy, so a blocked request arrives as a
visible request to widen it — the demand signal the n=28 measurement behind this epic could
not supply.
"""

import ipaddress
import socket
import urllib.parse

HOST_POLICY_NAME = "the url-fetcher host allow-list"
ADDRESS_POLICY_NAME = "the url-fetcher address-class policy (global unicast addresses only)"
SCHEME_POLICY_NAME = "the url-fetcher scheme policy (https only)"
PORT_POLICY_NAME = "the url-fetcher port policy"
METADATA_POLICY_NAME = "the url-fetcher metadata-endpoint deny-list"

# the hosts the fetcher dials when the deployment sets no allow-list of its own. Widening it
# is a deliberate change here, and a refusal names this policy so the demand arrives as a
# request rather than a dead end.
DEFAULT_ALLOWED_HOSTS = (
    "raw.githubusercontent.com",
    "github.com",
    "zenodo.org",
    "ftp.ebi.ac.uk",
    "eutils.ncbi.nlm.nih.gov",
)

DEFAULT_ALLOWED_PORTS = (443,)

# refused by NAME as well as by address, because the name is the form an attacker writes and
# a resolver that answers it differently must not be the only thing standing in the way
METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
})

_LOOPBACK_LITERALS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class Refused(Exception):
    """A request this service will not make. `type` is the wire error type; `status` is the
    HTTP status the surface answers with."""

    def __init__(self, type, message, *, status=403, retryable=False, details=None):
        super().__init__(message)
        self.type = type
        self.message = message
        self.status = status
        self.retryable = retryable
        # extra wire fields for the one error that carries evidence: what the upstream
        # actually served, reported as-is rather than sniffed
        self.details = details or {}


class Target:
    """One validated hop: the address to CONNECT to and the hostname TLS must verify."""

    __slots__ = ("url", "scheme", "host", "port", "address", "path")

    def __init__(self, url, scheme, host, port, address, path):
        self.url = url
        self.scheme = scheme
        self.host = host
        self.port = port
        self.address = address
        self.path = path

    def __repr__(self):
        return f"<Target {self.scheme}://{self.host}:{self.port} at {self.address}>"


def _unwrap(ip):
    """The address an IPv6 form actually reaches. ::ffff:10.0.0.1, 2002::/16 and Teredo all
    carry an IPv4 address that the kernel, not this check, is what finally talks to.

    NAT64 is deliberately NOT unwrapped here, because it does not need to be: the well-known
    prefix 64:ff9b::/96 sits inside ::/8, which `ipaddress` reports as reserved, so every
    address under it is refused by `address_class`'s reserved arm whatever it embeds —
    measured, not assumed, and locked by a test. The consequence is that the message names
    "reserved" rather than the embedded class; that is the whole cost."""
    if ip.version != 6:
        return ip
    for attr in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(ip, attr, None)
        if embedded is not None:
            return embedded
    teredo = getattr(ip, "teredo", None)
    if teredo:
        return teredo[1]
    return ip


def address_class(ip):
    """The reason `ip` is not a global unicast address, or None if it is one.

    Ordered most-specific first so the message names the class a reader recognises rather than
    the catch-all. `is_global` alone would do most of this, but it has moved between Python
    releases and this is the one check the epic's kill criterion is written against."""
    ip = _unwrap(ip)
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (169.254.0.0/16 and fe80::/10 — the cloud metadata endpoint)"
    if ip.is_multicast:
        return "multicast"
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "CGNAT (100.64.0.0/10)"
    if ip.is_private:
        return "private (RFC1918 and the other reserved-for-private ranges)"
    if ip.is_reserved:
        return "reserved"
    if not ip.is_global:
        return "not global unicast"
    return None


class Guard:
    """Decides whether a URL may be fetched, and what address the fetch must connect to.

    `allow_loopback_origin` is the development affordance and the ONLY parameter that relaxes
    anything. It unlocks a loopback origin — a loopback literal host, a plain-http scheme and
    an arbitrary port, all three only when the resolved address is itself loopback. It unlocks
    no other address class."""

    def __init__(self, allowed_hosts=DEFAULT_ALLOWED_HOSTS, *,
                 allowed_ports=DEFAULT_ALLOWED_PORTS, allow_loopback_origin=False,
                 resolver=None):
        self.allowed_hosts = tuple(h.strip().lower() for h in allowed_hosts if h.strip())
        self.allowed_ports = tuple(allowed_ports)
        self.allow_loopback_origin = bool(allow_loopback_origin)
        # injected only so a test can resolve without a resolver; production leaves it None
        self._resolver = resolver or self._getaddrinfo

    # -- host policy ---------------------------------------------------------------------

    def host_allowed(self, host):
        """Exact match, or a suffix match for an entry written with a leading dot."""
        host = host.lower()
        for entry in self.allowed_hosts:
            if entry.startswith("."):
                if host == entry[1:] or host.endswith(entry):
                    return True
            elif host == entry:
                return True
        return False

    def _policy_statement(self):
        return f"{HOST_POLICY_NAME}: {', '.join(self.allowed_hosts) or '(empty)'}"

    # -- resolution ----------------------------------------------------------------------

    @staticmethod
    def _getaddrinfo(host, port):
        return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)

    def _resolve(self, host, port):
        try:
            infos = self._resolver(host, port)
        except socket.gaierror as exc:
            raise Refused("unreachable", f"{host} does not resolve: {exc}",
                          status=502, retryable=True) from None
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addresses:
            raise Refused("unreachable", f"{host} resolved to no usable address",
                          status=502, retryable=True)
        return addresses

    # -- the whole check, run per hop ----------------------------------------------------

    def check(self, url):
        """A validated Target, or Refused. Runs in full on the requested URL and again on
        every redirect hop: a permitted origin that redirects inward is the case the address
        check exists for."""
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise Refused("refused_by_policy", f"not a parseable URL: {exc}") from None

        scheme = (parts.scheme or "").lower()
        if scheme not in ("https", "http"):
            raise Refused("refused_by_policy",
                          f"scheme {scheme or '(none)'!r} is refused by {SCHEME_POLICY_NAME}")
        if scheme == "http" and not self.allow_loopback_origin:
            raise Refused("refused_by_policy", f"http is refused by {SCHEME_POLICY_NAME}")

        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise Refused("refused_by_policy",
                          "a URL carrying userinfo is refused: credentials in a URL are a "
                          "way to steer a request at a host that reads them")

        host = (parts.hostname or "").strip().lower()
        if not host:
            raise Refused("refused_by_policy", "the URL names no host")
        try:
            host.encode("ascii")
        except UnicodeEncodeError:
            raise Refused("refused_by_policy",
                          "a non-ASCII host is refused; supply the punycode form") from None
        if host in METADATA_HOSTNAMES:
            raise Refused("refused_by_policy",
                          f"{host} is refused by {METADATA_POLICY_NAME}")

        # an address written as a literal is classified HERE, before the host allow-list, so
        # that an https URL naming 169.254.169.254 or 10.0.0.1 is refused by the address-class
        # policy and says so — including when an operator has put that literal IN the
        # allow-list, which is the case this ordering exists for and the one the kill
        # criterion is written against. The scheme gate above answers first and refuses every
        # http:// URL before this runs, so the address class is what refuses these only for
        # the https spellings; on the permissive instance, where http:// gets past the scheme
        # gate, it is what refuses them there too.
        literal = None
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        if literal is not None:
            reason = address_class(literal)
            if reason is not None and not (reason == "loopback" and self.allow_loopback_origin):
                raise Refused("refused_by_policy",
                              f"{literal} is refused by {ADDRESS_POLICY_NAME}: {reason}")

        loopback_literal = self.allow_loopback_origin and host in _LOOPBACK_LITERALS
        if not loopback_literal and not self.host_allowed(host):
            # named, not merely refused: this message is how a wanted host becomes a request
            # to widen the policy instead of an invisible dead end
            raise Refused("refused_by_policy",
                          f"{host} is not in {self._policy_statement()}. Widening it is a "
                          "configuration change to the url-fetcher deployment.")

        try:
            port = parts.port
        except ValueError:
            raise Refused("refused_by_policy", "the URL names an unusable port") from None
        if port is None:
            port = 443 if scheme == "https" else 80
        if port not in self.allowed_ports and not loopback_literal:
            raise Refused("refused_by_policy",
                          f"port {port} is refused by {PORT_POLICY_NAME}: "
                          f"{', '.join(str(p) for p in self.allowed_ports)} only")

        addresses = self._resolve(host, port)
        # every answer is checked, not only the one we connect to: a resolver that returns a
        # public and a private address must not be able to have the private one picked later
        for ip in addresses:
            reason = address_class(ip)
            if reason is None:
                continue
            if reason == "loopback" and self.allow_loopback_origin:
                continue
            # the address itself is NOT reported: naming it would make this service a
            # DNS-resolution oracle for every allow-listed host and put an internal address
            # into a chat transcript. The class and the policy are named, which is what turns
            # a refusal into a request to widen the policy.
            raise Refused("refused_by_policy",
                          f"{host} resolves to an address that {ADDRESS_POLICY_NAME} "
                          f"refuses: {reason}")

        address = addresses[0]
        if scheme == "http" and not _unwrap(address).is_loopback:
            # the allowance let plain http past the scheme gate; it is only ever for the
            # loopback origin the local proving ground serves
            raise Refused("refused_by_policy",
                          f"http is refused by {SCHEME_POLICY_NAME} for {host}")

        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        return Target(url, scheme, host, port, address, path)
