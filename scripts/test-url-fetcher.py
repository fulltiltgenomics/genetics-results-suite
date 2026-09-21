#!/usr/bin/env python3
"""Adversarial assertions about url-fetcher/guard.py and url-fetcher/fetch.py.

Run: python3 scripts/test-url-fetcher.py
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

Offline in the strong sense: a local origin server on loopback, a stubbed resolver, and a
recording `socket.create_connection` installed for the whole run that serves loopback and
REFUSES every other destination. Nothing this file does can reach a network, and the refused
destinations are the evidence — when a control is removed, the probe's red is "the fetcher
asked to dial 169.254.169.254", recorded at the socket layer before anything left the host.

TWO CONVENTIONS, and they are what make this evidence rather than decoration:

* EVERY control is DRIVEN AS THE FAILURE. For each hazard asserted closed, the specific line
  that closes it is cut out of a private copy of the module source, compiled into a mutant
  module in this process, and the same probe is asserted to go RED against it. A cut whose
  anchor no longer matches the source exits 2 rather than passing quietly, so a refactor
  cannot turn a positive control into a no-op.
* The cut lives ENTIRELY IN THIS PROCESS. There is no environment variable, flag or config
  key that relaxes anything — url-fetcher/main.py defines no CLI flags and guard.py reads no
  environment, and a test hook that changed that would hand back the attack surface the
  design closed. The mutants are built by text substitution on a source string; nothing
  shipped in the image can construct one, and the permissive guard is reachable only by
  passing `allow_loopback_origin=True` in-process, which is what url-fetcher/devserver.py
  does and what nothing in the image does.

Anything about a HANG is driven on a thread with a deadline, so a regression fails the check
rather than wedging the harness.
"""

import base64
import gzip
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "url-fetcher")
sys.path.insert(0, SRC)

try:
    import fetch
    import guard
    import server as surface
except Exception as exc:  # pragma: no cover - harness failure
    print(f"HARNESS: cannot import url-fetcher modules from {SRC}: {exc}", file=sys.stderr)
    sys.exit(2)


# -- counters ---------------------------------------------------------------------------

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL  {name} {detail}")
    else:
        print(f"  ok    {name}")


def harness_error(message):
    print(f"HARNESS: {message}", file=sys.stderr)
    sys.exit(2)


# -- the connect recorder ----------------------------------------------------------------

DIALLED = []
_real_create_connection = socket.create_connection


def _recording_create_connection(address, timeout=None, *args, **kwargs):
    """Loopback is served; everything else is RECORDED and refused.

    This is the harness's containment and its instrument at once. A guard that fails open
    does not reach the network from here — it leaves a line in DIALLED naming the address it
    wanted, which is what the negative controls assert on."""
    host, port = address[0], address[1]
    DIALLED.append((str(host), int(port)))
    try:
        loopback = ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConnectionRefusedError(f"harness: refusing a non-loopback connect to {host}")
    return _real_create_connection(address, timeout, *args, **kwargs)


def dialled_since(mark):
    return DIALLED[mark:]


def dialled_any(mark, host):
    return any(h == host for h, _ in dialled_since(mark))


# -- the local origin --------------------------------------------------------------------

BODY = b"trait\tbeta\tse\nT2D\t0.31\t0.04\n"
BIG_TOTAL = 8 * 1024 * 1024
BIG_CHUNK = 32 * 1024
# the first megabyte is paced so that a fetcher which refuses on the declared length has time
# to close before the kernel's loopback send buffer has swallowed more than the cap; without
# it the "how many bytes did the origin have to send" assertion measures buffer size instead
PACED_CHUNKS = 32
REDIRECT_CHAIN = 9
# the truncated-body fixture: a declared length far larger than what is sent, then a close
SHORT_DECLARED = 400000
SHORT_SENT = b"trunc"
# a gzip FILE (what a .tsv.gz is) versus a gzip TRANSFER ENCODING nobody asked for. The bytes
# are identical; only the Content-Encoding header tells them apart, which is the whole point
GZ_BYTES = gzip.compress(BODY)
# 40 header lines at half a second each: 20s of header phase against a 2s budget, which a
# per-recv timeout cannot see because every line resets it
DRIP_HEADERS = 40
DRIP_INTERVAL = 0.5


class Origin(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.connections = 0
        self.body_bytes = 0
        self._lock = threading.Lock()

    @property
    def port(self):
        return self.server_address[1]

    def note_sent(self, n):
        with self._lock:
            self.body_bytes += n

    def reset(self):
        with self._lock:
            self.requests, self.connections, self.body_bytes = [], 0, 0


class OriginHandler(socketserver.StreamRequestHandler):
    timeout = 20

    def handle(self):
        self.server.connections += 1
        try:
            line = self.rfile.readline(65536).decode("latin-1").strip()
        except OSError:
            return
        if not line:
            return
        parts = line.split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]
        headers = {}
        while True:
            raw = self.rfile.readline(65536).decode("latin-1")
            if raw in ("\r\n", "\n", ""):
                break
            key, _, value = raw.partition(":")
            headers[key.strip().lower()] = value.strip()
        self.server.requests.append((method, path, headers))
        self.route(method, path)

    # -- responses -----------------------------------------------------------------------

    def _send(self, status, headers, body=b""):
        head = f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers)
        head += "Connection: close\r\n\r\n"
        try:
            self.wfile.write(head.encode("latin-1") + body)
            self.wfile.flush()
        except OSError:
            return
        self.server.note_sent(len(body))

    def _stream(self, headers, chunked):
        head = "HTTP/1.1 200 OK\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers)
        head += "Connection: close\r\n\r\n"
        try:
            self.wfile.write(head.encode("latin-1"))
            self.wfile.flush()
        except OSError:
            return
        sent, n = 0, 0
        payload = b"x" * BIG_CHUNK
        while sent < BIG_TOTAL:
            block = (f"{len(payload):x}\r\n".encode() + payload + b"\r\n") if chunked else payload
            try:
                self.wfile.write(block)
                self.wfile.flush()
            except OSError:
                break
            sent += len(payload)
            self.server.note_sent(len(payload))
            n += 1
            if n <= PACED_CHUNKS:
                time.sleep(0.02)
        if chunked:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

    def route(self, method, path):
        base = path.split("?", 1)[0]
        if base in ("/ok", "/victim", "/"):
            body = b"PRIVATE-DATA-THE-FETCHER-MUST-NEVER-SEE\n" if base == "/victim" else BODY
            # Set-Cookie and Location are here so the surface group can assert that nothing
            # the upstream says comes back out of POST /fetch as a header
            self._send("200 OK", [("Content-Type", "text/tab-separated-values"),
                                  ("Content-Length", str(len(body))),
                                  ("Set-Cookie", "upstream_sid=leak; Path=/"),
                                  ("Location", "https://elsewhere.example/")], body)
        elif base == "/big-declared":
            self._stream([("Content-Type", "application/octet-stream"),
                          ("Content-Length", str(BIG_TOTAL))], chunked=False)
        elif base == "/big-chunked":
            # declares nothing about its size at all
            self._stream([("Content-Type", "application/octet-stream"),
                          ("Transfer-Encoding", "chunked")], chunked=True)
        elif base == "/big-lying":
            # declares 12 bytes and sends 8 MiB; the chunked framing is what the client obeys
            self._stream([("Content-Type", "application/octet-stream"),
                          ("Content-Length", "12"),
                          ("Transfer-Encoding", "chunked")], chunked=True)
        elif base == "/short-body":
            # declares 400000 and sends 5, then closes. CPython's HTTPResponse.read(amt)
            # answers this with b"" rather than IncompleteRead, so only a comparison against
            # the declared length can tell it from a complete file
            self._send("200 OK", [("Content-Type", "text/tab-separated-values"),
                                  ("Content-Length", str(SHORT_DECLARED))], SHORT_SENT)
        elif base == "/encoding-gzip":
            self._send("200 OK", [("Content-Type", "text/tab-separated-values"),
                                  ("Content-Encoding", "gzip"),
                                  ("Content-Length", str(len(GZ_BYTES)))], GZ_BYTES)
        elif base == "/gz-file":
            # the common case: a gzipped FILE, no Content-Encoding. Must still fetch, and the
            # bytes must arrive byte-identical
            self._send("200 OK", [("Content-Type", "application/gzip"),
                                  ("Content-Length", str(len(GZ_BYTES)))], GZ_BYTES)
        elif base == "/drip-headers":
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.flush()
                for i in range(DRIP_HEADERS):
                    time.sleep(DRIP_INTERVAL)
                    self.wfile.write(f"X-Pad-{i}: x\r\n".encode("latin-1"))
                    self.wfile.flush()
                self.wfile.write(b"Content-Length: 0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
        elif base == "/hang":
            time.sleep(12)
        elif base == "/redirect-literal":
            self._send("302 Found", [("Location", "https://10.0.0.1/secret"),
                                     ("Content-Length", "0")])
        elif base == "/redirect-metadata":
            self._send("302 Found",
                       [("Location", "http://169.254.169.254/computeMetadata/v1/"),
                        ("Content-Length", "0")])
        elif base == "/redirect-name":
            self._send("302 Found", [("Location", "https://github.com/inside.tsv"),
                                     ("Content-Length", "0")])
        elif base.startswith("/redir/"):
            n = int(base.rsplit("/", 1)[1])
            if n >= REDIRECT_CHAIN:
                self._send("200 OK", [("Content-Length", str(len(BODY)))], BODY)
            else:
                self._send("302 Found", [("Location", f"/redir/{n + 1}"),
                                         ("Content-Length", "0")])
        else:
            self._send("404 Not Found", [("Content-Length", "0")])


def start_origin():
    origin = Origin(("127.0.0.1", 0), OriginHandler)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    return origin


# -- stubbed resolvers -------------------------------------------------------------------

def stub_resolver(mapping, calls=None):
    """getaddrinfo's shape, answered from a table. `calls` collects the hostnames asked for,
    which is how the pinning check counts resolutions."""
    def resolve(host, port):
        if calls is not None:
            calls.append(host)
        answers = mapping.get(host)
        if answers is None:
            raise socket.gaierror(socket.EAI_NONAME, f"stub: no answer for {host}")
        if isinstance(answers, str):
            answers = [answers]
        out = []
        for address in answers:
            ip = ipaddress.ip_address(address)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            sockaddr = (address, port, 0, 0) if ip.version == 6 else (address, port)
            out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return out
    return resolve


def rebinding_resolver(first, then, calls):
    """Public on the check, private on the connect: the resolver a rebinding attack uses."""
    def resolve(host, port):
        calls.append(host)
        address = first if len(calls) == 1 else then
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (address, port))]
    return resolve


# -- mutants: a control cut out of a private copy of the source ---------------------------

GUARD_CUTS = {
    "scheme-https": [
        ('        if scheme == "http" and not self.allow_loopback_origin:', "        if False:"),
        ('        if scheme == "http" and not _unwrap(address).is_loopback:', "        if False:"),
    ],
    "userinfo": [
        ('        if parts.username is not None or parts.password is not None or "@" in parts.netloc:',
         "        if False:"),
    ],
    "metadata-names": [
        ("        if host in METADATA_HOSTNAMES:", "        if False:"),
    ],
    "address-class-literal": [
        ("        if literal is not None:", "        if False:"),
    ],
    "address-class-resolved": [
        ("            reason = address_class(ip)", "            reason = None"),
    ],
    "host-allow-list": [
        ("        if not loopback_literal and not self.host_allowed(host):", "        if False:"),
    ],
    "port-policy": [
        ("        if port not in self.allowed_ports and not loopback_literal:", "        if False:"),
    ],
}

FETCH_CUTS = {
    "redirect-cap": [
        ("                    if redirects >= self.max_redirects:", "                    if False:"),
    ],
    "declared-length-cap": [
        ("                if declared and declared.isdigit() and int(declared) > self.max_bytes:",
         "                if False:"),
    ],
    "stream-cap": [
        ("        if total > max_bytes:", "        if False:"),
    ],
    "wall-clock": [
        ("        if time.monotonic() > deadline:", "        if False:"),
        ("        conn.sock = _DeadlineSocket(conn.sock, deadline)",
         "        conn.sock.settimeout(None)"),
    ],
    "header-deadline": [
        # the shape this replaced: a PER-RECV timeout rather than a wall clock. The body-phase
        # deadline check is left in place, so the probe isolates the header phase.
        ("        conn.sock = _DeadlineSocket(conn.sock, deadline)",
         "        conn.sock.settimeout(max(0.1, deadline - time.monotonic()))"),
    ],
    "truncated-body": [
        ("            if expected is not None and total < expected:", "            if False:"),
    ],
    "content-encoding": [
        ('                if encoding and encoding.lower() != "identity":',
         "                if False:"),
    ],
}

SERVER_CUTS = {
    "close-on-unread-body": [
        ("        if length > MAX_REQUEST_BYTES:\n            self.close_connection = True",
         "        if length > MAX_REQUEST_BYTES:\n            pass"),
    ],
    "json-envelope": [
        ("    def send_error(self, code, message=None, explain=None):",
         "    def _cut_send_error(self, code, message=None, explain=None):"),
    ],
    "health-allowed-hosts": [
        ('"allowed_hosts": list(self.fetcher.guard.allowed_hosts)',
         '"allowed_hosts": []'),
    ],
}

_MUTANTS = {}


def _mutate(path, table, names, modname):
    key = (modname, names)
    if key in _MUTANTS:
        return _MUTANTS[key]
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    for name in names:
        for anchor, replacement in table[name]:
            if source.count(anchor) != 1:
                harness_error(
                    f"the cut {name!r} no longer matches {os.path.basename(path)} "
                    f"({source.count(anchor)} occurrences of its anchor). The positive "
                    f"control cannot be built, so this run proves nothing — fix the anchor.")
            source = source.replace(anchor, replacement)
    module = types.ModuleType(modname)
    module.__file__ = path
    try:
        exec(compile(source, f"{path} [cut: {', '.join(names)}]", "exec"), module.__dict__)
    except Exception as exc:
        harness_error(f"the mutant {modname} does not compile: {exc}")
    _MUTANTS[key] = module
    return module


def mutant_guard(*names, **kwargs):
    module = _mutate(os.path.join(SRC, "guard.py"), GUARD_CUTS, names, "guard_cut")
    # the mutant defines its own Refused; rebinding the name makes its raises use the real
    # class, so `except guard.Refused` in this file catches them
    module.Refused = guard.Refused
    return module.Guard(**kwargs)


def mutant_fetcher(names, guard_obj, **kwargs):
    module = _mutate(os.path.join(SRC, "fetch.py"), FETCH_CUTS, tuple(names), "fetch_cut")
    return module.Fetcher(guard_obj, **kwargs)


def mutant_surface(*names):
    """The HTTP surface with one of its controls cut, ready to be served on a port."""
    return _mutate(os.path.join(SRC, "server.py"), SERVER_CUTS, names, "server_cut")


def mutant_main(*guard_cuts):
    """url-fetcher/main.py compiled against a guard whose address-class check has been cut.
    The tripwire in main.py exists to notice exactly that, so this is its positive control."""
    mg = _mutate(os.path.join(SRC, "guard.py"), GUARD_CUTS, guard_cuts, "guard_cut")
    mg.Refused = guard.Refused
    module = types.ModuleType("main_cut")
    path = os.path.join(SRC, "main.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    real = sys.modules.get("guard")
    sys.modules["guard"] = mg
    try:
        exec(compile(source, f"{path} [guard cut: {', '.join(guard_cuts)}]", "exec"),
             module.__dict__)
    finally:
        sys.modules["guard"] = real
    return module


# -- probe helpers -----------------------------------------------------------------------

def refusal(fn):
    """(Refused, None) or (None, value)."""
    try:
        return None, fn()
    except guard.Refused as exc:
        return exc, None


def expect_refused(name, fn, contains=None, type_=None):
    exc, value = refusal(fn)
    if exc is None:
        check(name, False, f"NOT refused; returned {value!r}")
        return None
    ok, detail = True, f"{exc.type}: {exc.message}"
    if type_ is not None and exc.type != type_:
        ok = False
    if contains is not None and contains not in exc.message:
        ok = False
    check(name, ok, "" if ok else f"got {detail}")
    return exc


def run_with_deadline(fn, seconds):
    """A hang is measured, never waited on. Returns (finished, outcome)."""
    box = {}

    def body():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported, not handled
            box["error"] = exc
        box["done"] = True

    thread = threading.Thread(target=body, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(seconds)
    return (not thread.is_alive()), box, time.monotonic() - started


def production_guard(**kwargs):
    return guard.Guard(**kwargs)


def permissive_guard(**kwargs):
    """The construction url-fetcher/devserver.py makes, and the only one that relaxes
    anything. Reachable in-process only — no manifest can produce it."""
    kwargs.setdefault("allow_loopback_origin", True)
    return guard.Guard(**kwargs)


# =========================================================================================
# groups
# =========================================================================================

def test_scheme_and_userinfo():
    g = production_guard()
    expect_refused("http:// is refused",
                   lambda: g.check("http://raw.githubusercontent.com/f.tsv"),
                   contains=guard.SCHEME_POLICY_NAME)
    expect_refused("ftp:// is refused",
                   lambda: g.check("ftp://ftp.ebi.ac.uk/f.tsv"),
                   contains=guard.SCHEME_POLICY_NAME)
    expect_refused("file:// is refused", lambda: g.check("file:///etc/passwd"),
                   contains=guard.SCHEME_POLICY_NAME)
    for url in ("https://user:pw@raw.githubusercontent.com/f.tsv",
                "https://raw.githubusercontent.com@169.254.169.254/",
                "https://token@github.com/f.tsv"):
        expect_refused(f"userinfo refused: {url}", lambda u=url: g.check(u),
                       contains="userinfo")

    # CONTROL: cut the scheme policy — both gates, since the second catches what the first
    # would let past — and http:// to a public host walks through. The port policy is a
    # DIFFERENT control and would answer first for http's default port 80, so it is widened
    # by configuration here rather than cut: a positive control has to isolate one control.
    m = mutant_guard("scheme-https", allowed_ports=(80,),
                     resolver=stub_resolver({"raw.githubusercontent.com": "185.199.108.153"}))
    exc, target = refusal(lambda: m.check("http://raw.githubusercontent.com/f.tsv"))
    check("CONTROL scheme-https cut: http:// is accepted",
          exc is None and target is not None and target.scheme == "http",
          f"still refused: {exc and exc.message}")

    # CONTROL: cut the userinfo check and a credentialed URL is accepted
    m = mutant_guard("userinfo",
                     resolver=stub_resolver({"raw.githubusercontent.com": "185.199.108.153"}))
    exc, target = refusal(lambda: m.check("https://user:pw@raw.githubusercontent.com/f.tsv"))
    check("CONTROL userinfo cut: a credentialed URL is accepted", exc is None,
          f"still refused: {exc and exc.message}")


def test_metadata_endpoint():
    g = production_guard()
    # by address, and the message must name the ADDRESS policy rather than the allow-list
    exc = expect_refused("169.254.169.254 refused by the address class",
                         lambda: g.check("https://169.254.169.254/computeMetadata/v1/"),
                         contains=guard.ADDRESS_POLICY_NAME)
    if exc:
        check("the 169.254.169.254 refusal names link-local, not the host allow-list",
              guard.HOST_POLICY_NAME not in exc.message, exc.message)
    expect_refused("[fe80::1] refused (IPv6 link-local)",
                   lambda: g.check("https://[fe80::1]/"),
                   contains=guard.ADDRESS_POLICY_NAME)
    expect_refused("[::ffff:169.254.169.254] refused (IPv4-mapped link-local)",
                   lambda: g.check("https://[::ffff:169.254.169.254]/"),
                   contains=guard.ADDRESS_POLICY_NAME)
    for name in ("metadata.google.internal", "metadata.goog", "metadata"):
        expect_refused(f"{name} refused by name",
                       lambda n=name: g.check(f"https://{n}/computeMetadata/v1/"),
                       contains=guard.METADATA_POLICY_NAME)

    # the name is refused even when an operator has allow-listed it and the resolver answers
    allowed = production_guard(
        allowed_hosts=("metadata.google.internal",),
        resolver=stub_resolver({"metadata.google.internal": "169.254.169.254"}))
    expect_refused("metadata.google.internal refused even when allow-listed",
                   lambda: allowed.check("https://metadata.google.internal/"),
                   contains=guard.METADATA_POLICY_NAME)

    # a host that is not a parseable literal but that a resolver turns into the metadata
    # endpoint — the decimal form of 169.254.169.254
    decimal = production_guard(allowed_hosts=("2852039166",),
                               resolver=stub_resolver({"2852039166": "169.254.169.254"}))
    expect_refused("a decimal host resolving to the metadata endpoint is refused",
                   lambda: decimal.check("https://2852039166/"),
                   contains=guard.ADDRESS_POLICY_NAME)

    # CONTROL, in two steps. Cutting the name deny-list alone leaves the address class
    # holding it, which is the layering; cutting both lets it through.
    m = mutant_guard("metadata-names", allowed_hosts=("metadata.google.internal",),
                     resolver=stub_resolver({"metadata.google.internal": "169.254.169.254"}))
    exc, _ = refusal(lambda: m.check("https://metadata.google.internal/"))
    check("LAYERING metadata-names cut: the address class still refuses it",
          exc is not None and guard.ADDRESS_POLICY_NAME in exc.message,
          f"got {exc and exc.message}")
    m = mutant_guard("metadata-names", "address-class-literal", "address-class-resolved",
                     allowed_hosts=("metadata.google.internal",),
                     resolver=stub_resolver({"metadata.google.internal": "169.254.169.254"}))
    exc, target = refusal(lambda: m.check("https://metadata.google.internal/"))
    check("CONTROL both metadata controls cut: the metadata endpoint is accepted",
          exc is None and target is not None and str(target.address) == "169.254.169.254",
          f"still refused: {exc and exc.message}")

    # and the red at the socket layer: with the controls gone the fetcher dials it
    mark = len(DIALLED)
    f = fetch.Fetcher(m, timeout_s=5)
    refusal(lambda: f.fetch("https://metadata.google.internal/computeMetadata/v1/"))
    check("CONTROL both metadata controls cut: 169.254.169.254 is DIALLED",
          dialled_any(mark, "169.254.169.254"), f"dialled {dialled_since(mark)}")


def test_loopback_and_private_literals():
    g = production_guard()
    literals = [
        ("https://127.0.0.1/f.tsv", "loopback"),
        ("https://127.0.0.53:443/f.tsv", "loopback"),
        ("https://[::1]/f.tsv", "loopback"),
        ("https://[::ffff:127.0.0.1]/f.tsv", "loopback (IPv4-mapped)"),
        ("https://10.0.0.1/f.tsv", "RFC1918"),
        ("https://172.16.3.4/f.tsv", "RFC1918"),
        ("https://192.168.1.1/f.tsv", "RFC1918"),
        ("https://[fd00::1]/f.tsv", "IPv6 ULA"),
        ("https://100.64.0.1/f.tsv", "CGNAT"),
        ("https://224.0.0.1/f.tsv", "multicast"),
        ("https://0.0.0.0/f.tsv", "unspecified"),
        ("https://[::ffff:10.1.2.3]/f.tsv", "IPv4-mapped RFC1918"),
        ("https://[64:ff9b::a00:1]/f.tsv", "NAT64-embedded RFC1918"),
        ("https://[64:ff9b::169.254.169.254]/f.tsv", "NAT64-embedded metadata endpoint"),
        ("https://[64:ff9b:1::]/f.tsv", "NAT64 local-use prefix"),
    ]
    for url, why in literals:
        expect_refused(f"{why}: {url}", lambda u=url: g.check(u),
                       contains=guard.ADDRESS_POLICY_NAME)

    # NAT64 is refused, but NOT by an unwrap: 64:ff9b::/96 sits inside ::/8, which
    # `ipaddress` reports as reserved, so address_class refuses it on the reserved arm
    # whatever it embeds. Two reviews disagreed about this; it is measured here so that a
    # change to either `_unwrap` or CPython's classification goes red rather than silent.
    check("NAT64 is refused on the reserved arm rather than by _unwrap",
          guard.address_class(ipaddress.ip_address("64:ff9b::a00:1")) == "reserved" and
          guard._unwrap(ipaddress.ip_address("64:ff9b::a00:1")) ==
          ipaddress.ip_address("64:ff9b::a00:1"),
          "the mechanism that refuses NAT64 has changed, and _unwrap's docstring with it")

    # localhost is a NAME, so the allow-list answers first; when it is allow-listed the
    # address class is what stops it, and that is the check the kill criterion names
    expect_refused("localhost refused (by the host allow-list)",
                   lambda: g.check("https://localhost/f.tsv"),
                   contains=guard.HOST_POLICY_NAME)
    allowed = production_guard(allowed_hosts=("localhost",),
                               resolver=stub_resolver({"localhost": ["127.0.0.1", "::1"]}))
    expect_refused("localhost refused by the address class when allow-listed",
                   lambda: allowed.check("https://localhost/f.tsv"),
                   contains=guard.ADDRESS_POLICY_NAME)

    # CONTROL: cut the address-class check at BOTH the points it runs — the literal and the
    # resolved answer. Cutting only the literal leaves the resolved check holding every one
    # of these, since getaddrinfo hands an IP literal straight back; that layering is
    # asserted on its own below in the ordering group.
    m = mutant_guard("address-class-literal", "address-class-resolved", "host-allow-list",
                     "port-policy")
    accepted = []
    for url, _ in literals:
        exc, target = refusal(lambda u=url: m.check(u))
        if exc is None:
            accepted.append(url)
    check("CONTROL address-class cut (literal and resolved): every private literal is accepted",
          len(accepted) == len(literals),
          f"{len(accepted)}/{len(literals)} accepted; still refused: "
          f"{sorted(set(u for u, _ in literals) - set(accepted))}")
    mark = len(DIALLED)
    f = fetch.Fetcher(m, timeout_s=5)
    refusal(lambda: f.fetch("https://10.0.0.1/f.tsv"))
    check("CONTROL address-class cut: 10.0.0.1 is DIALLED",
          dialled_any(mark, "10.0.0.1"), f"dialled {dialled_since(mark)}")


def test_name_resolving_to_rfc1918():
    calls = []
    g = production_guard(resolver=stub_resolver({"github.com": "10.11.12.13"}, calls))
    exc = expect_refused("an allow-listed name resolving to RFC1918 is refused",
                         lambda: g.check("https://github.com/f.tsv"),
                         contains=guard.ADDRESS_POLICY_NAME)
    if exc:
        # the address is deliberately NOT named: doing so makes the service a DNS-resolution
        # oracle for every allow-listed host and puts an internal address into a chat
        # transcript. The POLICY and the CLASS are named, which is what makes a refusal a
        # visible request to widen the policy.
        check("the refusal does NOT echo the resolved address back to the caller",
              "10.11.12.13" not in exc.message, exc.message)
        check("the refusal still names the policy and the class",
              guard.ADDRESS_POLICY_NAME in exc.message and "RFC1918" in exc.message,
              exc.message)
    check("the resolver was consulted", calls == ["github.com"], f"calls={calls}")

    # one public answer and one private one: the private answer must not be selectable later
    split = production_guard(
        resolver=stub_resolver({"github.com": ["185.199.108.153", "192.168.5.5"]}))
    expect_refused("a split-horizon answer (public first, private second) is refused",
                   lambda: split.check("https://github.com/f.tsv"),
                   contains=guard.ADDRESS_POLICY_NAME)

    # CONTROL: cut the resolved-address classification
    m = mutant_guard("address-class-resolved",
                     resolver=stub_resolver({"github.com": "10.11.12.13"}))
    exc, target = refusal(lambda: m.check("https://github.com/f.tsv"))
    check("CONTROL address-class-resolved cut: the RFC1918 answer is accepted",
          exc is None and target is not None and str(target.address) == "10.11.12.13",
          f"still refused: {exc and exc.message}")
    mark = len(DIALLED)
    refusal(lambda: fetch.Fetcher(m, timeout_s=5).fetch("https://github.com/f.tsv"))
    check("CONTROL address-class-resolved cut: 10.11.12.13 is DIALLED",
          dialled_any(mark, "10.11.12.13"), f"dialled {dialled_since(mark)}")


def test_rebinding_is_pinned(victim):
    """THE test. The resolver answers public while the guard looks and private once the
    socket is opened; only connecting to the address that was validated survives it."""
    public = "185.199.108.153"
    url = f"https://raw.githubusercontent.com:{victim.port}/victim"

    victim.reset()
    calls = []
    g = production_guard(allowed_hosts=("raw.githubusercontent.com",),
                         allowed_ports=(victim.port,),
                         resolver=rebinding_resolver(public, "127.0.0.1", calls))
    mark = len(DIALLED)
    exc, value = refusal(lambda: fetch.Fetcher(g, timeout_s=5).fetch(url))
    check("rebinding: the fetch does not return the private body",
          exc is not None and value is None, f"returned {value and value.content[:40]!r}")
    check("rebinding: the address DIALLED is the one the guard validated",
          dialled_any(mark, public), f"dialled {dialled_since(mark)}")
    check("rebinding: 127.0.0.1 was never dialled",
          not dialled_any(mark, "127.0.0.1"), f"dialled {dialled_since(mark)}")
    check("rebinding: the private origin received no connection at all",
          victim.connections == 0, f"{victim.connections} connections")
    check("rebinding: the name was resolved exactly once", len(calls) == 1, f"calls={calls}")

    # CONTROL: the collaborator every mainstream client library is — one that resolves again
    # inside its own connect path. Nothing else changes.
    victim.reset()
    calls = []
    g = production_guard(allowed_hosts=("raw.githubusercontent.com",),
                         allowed_ports=(victim.port,),
                         resolver=rebinding_resolver(public, "127.0.0.1", calls))
    real_connect = fetch._connect

    def unpinned_connect(target, deadline):
        rebound = g._resolve(target.host, target.port)[0]
        return real_connect(guard.Target(target.url, target.scheme, target.host, target.port,
                                         rebound, target.path), deadline)

    mark = len(DIALLED)
    fetch._connect = unpinned_connect
    try:
        refusal(lambda: fetch.Fetcher(g, timeout_s=5).fetch(url))
    finally:
        fetch._connect = real_connect
    check("CONTROL pinning removed: the rebound private address is DIALLED",
          dialled_any(mark, "127.0.0.1"), f"dialled {dialled_since(mark)}")
    check("CONTROL pinning removed: the private origin IS reached",
          victim.connections >= 1, f"{victim.connections} connections")
    check("CONTROL pinning removed: the name was resolved twice", len(calls) == 2,
          f"calls={calls}")


def test_redirects(origin):
    """A permitted origin that redirects inward is the case the per-hop re-check exists for.
    The origin here stands in for the allow-listed public host; the loopback allowance is what
    lets it be local, and it unlocks loopback only — the redirect targets stay refused."""
    base = f"http://127.0.0.1:{origin.port}"
    g = permissive_guard()
    f = fetch.Fetcher(g, timeout_s=10)

    origin.reset()
    result = None
    exc, result = refusal(lambda: f.fetch(f"{base}/redir/{REDIRECT_CHAIN - 2}"))
    check("a redirect chain within the cap is followed",
          exc is None and result is not None and result.content == BODY and
          result.redirects == 2, f"{exc and exc.message} redirects={result and result.redirects}")

    for path, why in (("/redirect-literal", "to an RFC1918 literal"),
                      ("/redirect-metadata", "to the metadata endpoint")):
        mark = len(DIALLED)
        exc = expect_refused(f"a redirect {why} is refused",
                             lambda p=path: f.fetch(f"{base}{p}"),
                             contains=guard.ADDRESS_POLICY_NAME)
        target = "10.0.0.1" if path == "/redirect-literal" else "169.254.169.254"
        check(f"a redirect {why} is not dialled", not dialled_any(mark, target),
              f"dialled {dialled_since(mark)}")

    # by name, not by literal: the redirect target is an allow-listed host whose resolver
    # answers RFC1918
    named = permissive_guard(
        resolver=stub_resolver({"github.com": "10.9.8.7", "127.0.0.1": "127.0.0.1"}))
    fn = fetch.Fetcher(named, timeout_s=10)
    mark = len(DIALLED)
    expect_refused("a redirect to a NAME resolving to RFC1918 is refused",
                   lambda: fn.fetch(f"{base}/redirect-name"),
                   contains=guard.ADDRESS_POLICY_NAME)
    check("that redirect target is not dialled", not dialled_any(mark, "10.9.8.7"),
          f"dialled {dialled_since(mark)}")

    expect_refused("a redirect chain longer than the cap is refused",
                   lambda: fetch.Fetcher(g, max_redirects=3, timeout_s=10).fetch(
                       f"{base}/redir/0"),
                   contains="redirects")

    # CONTROL 1: a guard that checks only the first hop — the collaborator a fetcher has if
    # it validates the requested URL and then hands the rest to a redirect-following client
    class OnlyFirstHop:
        def __init__(self, inner):
            self.inner = inner
            self.hops = 0

        def check(self, url):
            self.hops += 1
            if self.hops == 1:
                return self.inner.check(url)
            import urllib.parse
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname
            try:
                address = ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                address = ipaddress.ip_address(self.inner._resolve(host, 443)[0].compressed)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            return guard.Target(url, parts.scheme, host, port, address, path)

    mark = len(DIALLED)
    broken = OnlyFirstHop(permissive_guard())
    refusal(lambda: fetch.Fetcher(broken, timeout_s=5).fetch(f"{base}/redirect-literal"))
    check("CONTROL per-hop re-check removed: 10.0.0.1 is DIALLED",
          dialled_any(mark, "10.0.0.1"), f"dialled {dialled_since(mark)}")
    mark = len(DIALLED)
    broken = OnlyFirstHop(permissive_guard())
    refusal(lambda: fetch.Fetcher(broken, timeout_s=5).fetch(f"{base}/redirect-metadata"))
    check("CONTROL per-hop re-check removed: 169.254.169.254 is DIALLED",
          dialled_any(mark, "169.254.169.254"), f"dialled {dialled_since(mark)}")

    # CONTROL 2: cut the redirect cap and the chain runs past it
    mf = mutant_fetcher(("redirect-cap",), permissive_guard(), max_redirects=3, timeout_s=10)
    exc, result = refusal(lambda: mf.fetch(f"{base}/redir/0"))
    check("CONTROL redirect-cap cut: the chain runs past the cap",
          exc is None and result is not None and result.redirects > 3,
          f"{exc and exc.message} redirects={result and result.redirects}")


def test_byte_cap(origin):
    base = f"http://127.0.0.1:{origin.port}"
    g = permissive_guard()
    cap = 512 * 1024

    # (a) a declared oversize Content-Length: refused, and refused BEFORE the bytes are spent
    origin.reset()
    exc = expect_refused("an oversize declared Content-Length is refused",
                         lambda: fetch.Fetcher(g, max_bytes=cap, timeout_s=20).fetch(
                             f"{base}/big-declared"),
                         type_="too_large")
    if exc:
        check("the declared-length refusal is a 413", exc.status == 413, str(exc.status))
    sent = origin.body_bytes
    check("the declared-length refusal spends no bandwidth", sent < cap,
          f"the origin had to send {sent} bytes")

    # (b) a body that declares nothing and lies by omission: refused, not truncated
    for path, why in (("/big-chunked", "a chunked body declaring no length"),
                      ("/big-lying", "a chunked body declaring 12 bytes and sending 8 MiB")):
        origin.reset()
        exc, value = refusal(lambda p=path: fetch.Fetcher(g, max_bytes=cap, timeout_s=20)
                             .fetch(f"{base}{p}"))
        check(f"{why} is REFUSED",
              exc is not None and exc.type == "too_large",
              f"returned {value and len(value.content)} bytes; exc={exc and exc.type}")
        check(f"{why} is not truncated into a short success", value is None,
              f"returned a {value and len(value.content)}-byte Result")

    # CONTROL 1: cut the declared check alone. The stream cap still refuses — that is the
    # layering — but the whole oversize body now has to cross the wire.
    origin.reset()
    mf = mutant_fetcher(("declared-length-cap",), g, max_bytes=cap, timeout_s=20)
    exc, value = refusal(lambda: mf.fetch(f"{base}/big-declared"))
    check("LAYERING declared-length-cap cut: the stream cap still refuses",
          exc is not None and exc.type == "too_large", f"got {exc and exc.type} {value!r}")
    check("LAYERING declared-length-cap cut: the bytes are now spent",
          origin.body_bytes >= cap, f"the origin sent {origin.body_bytes} bytes")

    # CONTROL 2: cut both and the cap is gone — a body far over the cap is returned
    for path, why in (("/big-declared", "declared"), ("/big-chunked", "chunked")):
        origin.reset()
        mf = mutant_fetcher(("declared-length-cap", "stream-cap"), g, max_bytes=cap,
                            timeout_s=30)
        exc, value = refusal(lambda p=path: mf.fetch(f"{base}{p}"))
        check(f"CONTROL byte cap cut: the oversize {why} body is returned whole",
              exc is None and value is not None and len(value.content) > cap,
              f"{exc and exc.message} size={value and len(value.content)}")


def test_wall_clock(origin):
    base = f"http://127.0.0.1:{origin.port}"
    g = permissive_guard()

    finished, box, elapsed = run_with_deadline(
        lambda: fetch.Fetcher(g, timeout_s=2).fetch(f"{base}/hang"), 8)
    check("a host that hangs is cut off by the wall clock", finished,
          "the fetch was still running after 8s")
    exc = box.get("error")
    check("the hang is reported as timed_out",
          isinstance(exc, guard.Refused) and exc.type == "timed_out",
          f"got {type(exc).__name__ if exc else box.get('value')}")
    check("the hang is cut off near the deadline, not at the default", elapsed < 6,
          f"took {elapsed:.1f}s against a 2s cap")
    if isinstance(exc, guard.Refused):
        check("the timeout is a 504 and retryable", exc.status == 504 and exc.retryable,
              f"{exc.status} retryable={exc.retryable}")

    # CONTROL: cut the socket deadline and the read deadline, and the same probe hangs
    mf = mutant_fetcher(("wall-clock",), g, timeout_s=2)
    finished, box, elapsed = run_with_deadline(lambda: mf.fetch(f"{base}/hang"), 5)
    check("CONTROL wall-clock cut: the same probe hangs past the cap", not finished,
          f"finished in {elapsed:.1f}s with {box.get('error') or box.get('value')}")


def test_ordering_and_the_allowance(origin):
    """The two claims the implementer made that a reader cannot check by reading: that the
    literal address check runs BEFORE the host allow-list on both constructions, and that the
    loopback allowance unlocks loopback and nothing else."""
    dangerous = ["169.254.169.254", "10.0.0.1", "127.0.0.1", "192.168.0.1", "100.64.0.1",
                 "0.0.0.0", "::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "fd00::1",
                 "fe80::1", "224.0.0.1"]

    for label, g in (("production", production_guard(allowed_hosts=tuple(dangerous))),
                     ("permissive", permissive_guard(allowed_hosts=tuple(dangerous)))):
        for literal in dangerous:
            host = f"[{literal}]" if ":" in literal else literal
            exc, target = refusal(lambda h=host: g.check(f"https://{h}/x"))
            loopback_ok = (label == "permissive" and
                           guard.address_class(ipaddress.ip_address(literal)) == "loopback")
            if loopback_ok:
                # the allowance, and the only thing it unlocks
                check(f"{label}: {literal} is permitted by the loopback allowance",
                      exc is None or "resolves" in exc.message,
                      f"refused: {exc and exc.message}")
                continue
            ok = exc is not None and guard.ADDRESS_POLICY_NAME in exc.message
            check(f"{label}: {literal} is refused BY THE ADDRESS CHECK although allow-listed",
                  ok, f"got {exc and exc.message}")

    # the ordering claim, stated the other way round: with the address check gone, the
    # allow-list is what answers — which is what proves the address check answered first
    m = mutant_guard("address-class-literal", allowed_hosts=("169.254.169.254",),
                     resolver=stub_resolver({"169.254.169.254": "169.254.169.254"}))
    exc, _ = refusal(lambda: m.check("https://169.254.169.254/"))
    check("ORDERING: with the literal check cut, an allow-listed metadata literal is "
          "refused only by the resolved-address check",
          exc is not None and "resolves to" in exc.message, f"got {exc and exc.message}")

    # the allowance's blast radius: it unlocks a loopback ORIGIN, and plain http only to one
    g = permissive_guard(resolver=stub_resolver({"github.com": "185.199.108.153",
                                                 "127.0.0.1": "127.0.0.1"}))
    exc, target = refusal(lambda: g.check(f"http://127.0.0.1:{origin.port}/ok"))
    check("the allowance permits a plain-http loopback origin on any port", exc is None,
          f"refused: {exc and exc.message}")
    # on port 443 the port policy cannot answer first, so this isolates the second scheme
    # gate: the allowance lets plain http past the FIRST gate and the resolved address is
    # what refuses it. On port 80 it is refused too, by the port policy — asserted next.
    expect_refused("the allowance does NOT permit plain http to a public host",
                   lambda: g.check("http://github.com:443/f.tsv"),
                   contains=guard.SCHEME_POLICY_NAME)
    expect_refused("plain http to a public host on port 80 is refused as well",
                   lambda: g.check("http://github.com/f.tsv"))
    expect_refused("the allowance does NOT widen the host allow-list",
                   lambda: g.check("https://evil.example/f.tsv"),
                   contains=guard.HOST_POLICY_NAME)
    expect_refused("the allowance does NOT widen the port policy",
                   lambda: g.check("https://github.com:8443/f.tsv"),
                   contains=guard.PORT_POLICY_NAME)
    expect_refused("the allowance does NOT permit userinfo",
                   lambda: g.check("https://u:p@github.com/f.tsv"), contains="userinfo")

    # and the production entrypoint's own tripwire agrees with all of it
    import main as production_main
    ok = True
    try:
        production_main.assert_deployed_configuration(production_guard())
    except SystemExit as exc:
        ok = False
    check("main.assert_deployed_configuration passes for the deployed construction", ok)
    try:
        production_main.assert_deployed_configuration(permissive_guard())
        refused_permissive = False
    except SystemExit:
        refused_permissive = True
    check("main.assert_deployed_configuration REFUSES a permissive guard", refused_permissive)

    # LAYERING: cutting only the resolved-address check leaves the literal check holding the
    # tripwire, so it still passes — which is what shows the next control isolates one thing
    ok = True
    try:
        mutant_main("address-class-resolved").assert_deployed_configuration(production_guard())
    except SystemExit:
        ok = False
    check("LAYERING address-class-resolved cut: the tripwire still passes", ok)

    # CONTROL: delete the address-class check outright. The previous tripwire passed this —
    # every one of its probes was answered by the host allow-list or the scheme gate — which
    # is what made it vacuous. It must now refuse to start.
    try:
        mutant_main("address-class-literal",
                    "address-class-resolved").assert_deployed_configuration(production_guard())
        caught = False
    except SystemExit:
        caught = True
    check("CONTROL address-class cut: the deployed tripwire refuses to start", caught,
          "the tripwire passed with the address-class check deleted; it proves nothing")
    main_source = open(os.path.join(SRC, "main.py"), encoding="utf-8").read()
    check("the tripwire does not rely on `assert`, which PYTHONOPTIMIZE strips",
          re.search(r"^\s*assert\s", main_source, re.M) is None,
          "an env var a manifest can set would delete the tripwire")
    source = open(os.path.join(SRC, "main.py"), encoding="utf-8").read()
    check("main.py reads no environment variable that could relax the guard",
          "allow_loopback_origin=True" not in source and "argparse" not in source
          and "sys.argv" not in source,
          "the production entrypoint names a permissive construction or parses argv")


def test_surface(origin):
    """The HTTP surface, driven over the wire: the refusal reaches the caller as the stated
    contract, and nothing the upstream said comes back as a header."""
    fetcher = fetch.Fetcher(permissive_guard(), timeout_s=10)
    httpd = surface.make_server(fetcher, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    origin_url = f"http://127.0.0.1:{origin.port}/ok"

    def post(payload):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        body = json.dumps(payload).encode()
        conn.request("POST", "/fetch", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        headers = dict(response.getheaders())
        conn.close()
        return response.status, data, headers

    def health(target_port):
        conn = http.client.HTTPConnection("127.0.0.1", target_port, timeout=20)
        conn.request("GET", surface.HEALTH_PATH)
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        conn.close()
        return response.status, data

    try:
        # the prompt in chat-backend names the reachable hosts from this field; a health
        # route that stops carrying it leaves the model guessing and costs a refused fetch
        status, data = health(port)
        check("GET the health route carries the guard's allow-list",
              status == 200 and data.get("allowed_hosts") == list(fetcher.guard.allowed_hosts),
              str(data)[:160])
        check("the health route still reports status ok", data.get("status") == "ok",
              str(data)[:160])

        # CONTROL: cut the field's derivation and the answer no longer tracks the guard
        empty = mutant_surface("health-allowed-hosts").make_server(fetcher, "127.0.0.1", 0)
        threading.Thread(target=empty.serve_forever, daemon=True).start()
        try:
            _, cut_data = health(empty.server_address[1])
        finally:
            empty.shutdown()
            empty.server_close()
        check("CONTROL health-allowed-hosts cut: the health route stops tracking the guard",
              cut_data.get("allowed_hosts") != list(fetcher.guard.allowed_hosts),
              f"the cut answer still matched the guard: {cut_data}")

        status, data, headers = post({"url": origin_url})
        check("POST /fetch returns 200 for a permitted origin", status == 200, str(data))
        check("the body arrives byte-for-byte",
              base64.b64decode(data.get("content_b64", "")) == BODY, str(data)[:120])
        check("the sha256 is of those bytes",
              data.get("sha256") == hashlib.sha256(BODY).hexdigest(), str(data)[:120])
        check("no upstream Set-Cookie reaches the caller",
              not any(k.lower() == "set-cookie" for k in headers), str(headers))
        check("no upstream Location reaches the caller",
              not any(k.lower() == "location" for k in headers), str(headers))

        status, data, _ = post({"url": "https://169.254.169.254/computeMetadata/v1/"})
        check("POST /fetch refuses the metadata endpoint with 403", status == 403, str(data))
        check("the refusal names the address policy",
              guard.ADDRESS_POLICY_NAME in data.get("error", {}).get("message", ""), str(data))

        status, data, _ = post({"url": "https://evil.example/f.tsv"})
        check("a host outside the policy is refused by name of the policy",
              status == 403 and guard.HOST_POLICY_NAME in
              data.get("error", {}).get("message", ""), str(data))
        check("the refusal says how to widen it",
              "configuration change" in data.get("error", {}).get("message", ""), str(data))

        status, data, _ = post({"url": origin_url, "headers": {"X": "y"}})
        check("an unknown field is refused rather than ignored", status == 400, str(data))

        status, data, headers = post({"url": f"http://127.0.0.1:{origin.port}/short-body"})
        check("a truncated body reaches the caller as a failure, not a 200",
              status == 502 and data.get("error", {}).get("type") == "truncated", str(data))
        status, data, _ = post({"url": f"http://127.0.0.1:{origin.port}/encoding-gzip"})
        check("an unrequested Content-Encoding reaches the caller as a failure",
              status == 502 and
              data.get("error", {}).get("type") == "unrequested_encoding", str(data))

        # BLOCKING 7: the envelope is the contract for EVERY failure, including the verbs
        # this class defines no handler for — those fall through BaseHTTPRequestHandler's
        # send_error, whose default body is text/html
        for method in ("OPTIONS", "TRACE", "PROPFIND"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request(method, "/fetch")
            response = conn.getresponse()
            raw = response.read()
            conn.close()
            body = None
            try:
                body = json.loads(raw.decode())
            except ValueError:
                pass
            check(f"{method} is answered with the JSON envelope",
                  response.status == 501 and
                  response.getheader("Content-Type") == "application/json" and
                  isinstance(body, dict) and "error" in body,
                  f"{response.status} {response.getheader('Content-Type')} {raw[:80]!r}")
            check(f"the {method} envelope does not echo the request back",
                  raw.count(b"PROPFIND") == 0 and b"<html" not in raw.lower(), repr(raw[:80]))
        status, data, _ = post({"url": origin_url, "url2": "x"})
        check("a 400 after the body was read still answers the envelope",
              status == 400 and "error" in data, str(data))

        # BLOCKING 6: an internal bug is neither unreachable nor retryable
        class Exploding:
            guard = fetcher.guard

            def fetch(self, url):
                raise TypeError("an internal bug, not a network failure")

        real_fetcher = httpd.RequestHandlerClass.fetcher
        httpd.RequestHandlerClass.fetcher = Exploding()
        try:
            status, data, _ = post({"url": origin_url})
        finally:
            httpd.RequestHandlerClass.fetcher = real_fetcher
        error = data.get("error", {})
        check("an internal bug is not reported as a retryable network failure",
              status == 500 and error.get("type") == "internal_error" and
              error.get("retryable") is False, str(data))
        check("the internal error discloses the exception class and nothing more",
              error.get("message") == "the fetch failed inside the service: TypeError",
              str(error.get("message")))

        # BLOCKING 3: a refusal that never read the request body must not leave it framed
        # into the next request on a keep-alive connection
        def pipelined(target_port):
            oversize = json.dumps({"url": origin_url,
                                   "pad": "y" * (surface.MAX_REQUEST_BYTES + 1000)}).encode()
            good = json.dumps({"url": origin_url}).encode()
            wire = (b"POST /fetch HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(oversize)).encode() + b"\r\n\r\n" + oversize
                    + b"POST /fetch HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(good)).encode() + b"\r\n\r\n" + good)
            sock = socket.create_connection(("127.0.0.1", target_port), timeout=20)
            sock.sendall(wire)
            sock.settimeout(20)
            raw = b""
            try:
                while True:
                    block = sock.recv(65536)
                    if not block:
                        break
                    raw += block
            except OSError:
                pass
            sock.close()
            return raw

        raw = pipelined(port)
        check("a 413 that never read the body answers once and closes",
              raw.count(b"HTTP/1.1 ") == 1 and raw.startswith(b"HTTP/1.1 413"),
              f"{raw.count(b'HTTP/1.1 ')} responses on one connection: {raw[:160]!r}")
        check("the undrained body is never parsed as the next request line",
              b"Unsupported method" not in raw and b"501" not in raw, repr(raw[:200]))
        check("the caller is told the connection is closing",
              b"Connection: close" in raw, repr(raw[:200]))

        # CONTROL: cut the close and the leftover JSON is parsed as the next request
        broken = mutant_surface("close-on-unread-body").make_server(fetcher, "127.0.0.1", 0)
        threading.Thread(target=broken.serve_forever, daemon=True).start()
        try:
            raw = pipelined(broken.server_address[1])
        finally:
            broken.shutdown()
            broken.server_close()
        check("CONTROL close-on-unread-body cut: the connection desynchronises",
              raw.count(b"HTTP/1.1 ") != 1 or not raw.startswith(b"HTTP/1.1 413"),
              f"still framed correctly: {raw[:160]!r}")

        # CONTROL: cut the send_error override and an undefined verb gets HTML
        html = mutant_surface("json-envelope").make_server(fetcher, "127.0.0.1", 0)
        threading.Thread(target=html.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", html.server_address[1], timeout=20)
            conn.request("OPTIONS", "/fetch")
            response = conn.getresponse()
            body = response.read()
            content_type = response.getheader("Content-Type")
            conn.close()
        finally:
            html.shutdown()
            html.server_close()
        check("CONTROL json-envelope cut: an undefined verb gets an HTML error page",
              content_type is not None and "html" in content_type and b"<html" in body.lower(),
              f"{content_type} {body[:80]!r}")
    finally:
        httpd.shutdown()
        httpd.server_close()




def test_truncated_body(origin):
    """A length-delimited body that stops early. CPython answers `read(amt)` on one with b""
    and no IncompleteRead, so without an explicit comparison against the declared length the
    caller gets a 200, a size_bytes and a sha256 over bytes that are not the file."""
    base = f"http://127.0.0.1:{origin.port}"
    g = permissive_guard()

    origin.reset()
    exc, value = refusal(lambda: fetch.Fetcher(g, timeout_s=10).fetch(f"{base}/short-body"))
    check("a body that ends before its declared length is REFUSED",
          exc is not None and exc.type == "truncated",
          f"got {exc and exc.type}; returned {value and len(value.content)} bytes")
    check("the truncated body is not returned as a short success", value is None,
          f"returned a {value and len(value.content)}-byte Result")
    if exc is not None:
        check("the truncation is reported as retryable (a dropped connection, not a policy)",
              exc.status == 502 and exc.retryable, f"{exc.status} retryable={exc.retryable}")
        check("the refusal names both counts",
              str(len(SHORT_SENT)) in exc.message and str(SHORT_DECLARED) in exc.message,
              exc.message)

    # the chunked framing was never the exposed path; assert it stays closed
    origin.reset()
    exc, value = refusal(lambda: fetch.Fetcher(g, max_bytes=512 * 1024, timeout_s=20)
                         .fetch(f"{base}/big-lying"))
    check("the chunked path is still refused rather than truncated",
          exc is not None and value is None, f"returned {value and len(value.content)}")

    # CONTROL: cut the length comparison and the 5 bytes come back as a complete 200
    origin.reset()
    mf = mutant_fetcher(("truncated-body",), g, timeout_s=10)
    exc, value = refusal(lambda: mf.fetch(f"{base}/short-body"))
    check("CONTROL truncated-body cut: 5 bytes of a 400000-byte file are returned as a success",
          exc is None and value is not None and value.content == SHORT_SENT,
          f"{exc and exc.message} size={value and len(value.content)}")
    check("CONTROL truncated-body cut: the sha256 is over the truncated bytes, and nothing "
          "in the Result could tell the caller",
          value is not None and value.sha256 == hashlib.sha256(SHORT_SENT).hexdigest(),
          "the control did not reproduce the reported shape")


def test_content_encoding(origin):
    """The request asks for identity. An upstream is free to ignore that, and relaying what it
    sent turns a 48 KB response into 50 MB in the sandbox — a ~1000:1 amplifier past the cap."""
    base = f"http://127.0.0.1:{origin.port}"
    g = permissive_guard()

    origin.reset()
    exc, value = refusal(lambda: fetch.Fetcher(g, timeout_s=10).fetch(f"{base}/encoding-gzip"))
    check("a Content-Encoding that was not requested is REFUSED",
          exc is not None and exc.type == "unrequested_encoding",
          f"got {exc and exc.type}; returned {value and len(value.content)} bytes")
    if exc is not None:
        check("the unrequested encoding is not retryable", not exc.retryable,
              f"retryable={exc.retryable}")
        check("the refusal names the encoding it refused", "gzip" in exc.message, exc.message)
    check("the request asked for identity in the first place",
          any(h.get("accept-encoding") == "identity"
              for _, p, h in origin.requests if p.startswith("/encoding-gzip")),
          f"{[h.get('accept-encoding') for _, _, h in origin.requests]}")

    # the common case, and the one this must not break: a gzipped FILE carries no
    # Content-Encoding at all and must arrive byte-identical
    origin.reset()
    exc, value = refusal(lambda: fetch.Fetcher(g, timeout_s=10).fetch(f"{base}/gz-file"))
    check("a gzip FILE still fetches", exc is None and value is not None,
          f"refused: {exc and exc.message}")
    check("the gzip file arrives byte-identical",
          value is not None and value.content == GZ_BYTES,
          f"{value and len(value.content)} bytes against {len(GZ_BYTES)}")
    check("the gzip file reports no content_encoding",
          value is not None and value.content_encoding is None,
          f"content_encoding={value and value.content_encoding!r}")

    # CONTROL: cut the check and the unrequested encoding is relayed, tagged as gzip
    origin.reset()
    mf = mutant_fetcher(("content-encoding",), g, timeout_s=10)
    exc, value = refusal(lambda: mf.fetch(f"{base}/encoding-gzip"))
    check("CONTROL content-encoding cut: the compressed bytes are relayed",
          exc is None and value is not None and value.content == GZ_BYTES,
          f"{exc and exc.message} size={value and len(value.content)}")
    check("CONTROL content-encoding cut: the Result claims content_encoding gzip",
          value is not None and value.content_encoding == "gzip",
          f"content_encoding={value and value.content_encoding!r}")


def test_header_phase_deadline(origin):
    """settimeout is PER-RECV, and the header block is parsed inside http.client before any
    code in fetch.py regains control. An origin dripping one header line just inside the
    per-recv timeout resets the budget on every line."""
    base = f"http://127.0.0.1:{origin.port}"
    g = permissive_guard()
    drip_total = DRIP_HEADERS * DRIP_INTERVAL

    finished, box, elapsed = run_with_deadline(
        lambda: fetch.Fetcher(g, timeout_s=2).fetch(f"{base}/drip-headers"), 8)
    check("a drip-fed header block is cut off by the wall clock", finished,
          f"still running after 8s against a 2s budget (the drip lasts {drip_total:.0f}s)")
    exc = box.get("error")
    check("the header-phase timeout is reported as timed_out",
          isinstance(exc, guard.Refused) and exc.type == "timed_out",
          f"got {type(exc).__name__ if exc else box.get('value')}")
    check("the header phase is bounded by the BUDGET, not by budget x header count",
          elapsed < 6, f"took {elapsed:.1f}s against a 2s cap")
    if isinstance(exc, guard.Refused):
        check("the timeout message names the phase that spent the budget",
              "response headers" in exc.message, exc.message)

    # CONTROL: restore the per-recv timeout and the same probe runs past the budget
    mf = mutant_fetcher(("header-deadline",), g, timeout_s=2)
    finished, box, elapsed = run_with_deadline(lambda: mf.fetch(f"{base}/drip-headers"), 5)
    check("CONTROL header-deadline cut: the same probe runs past the budget", not finished,
          f"finished in {elapsed:.1f}s with {box.get('error') or box.get('value')}")


def test_filename_for():
    """The name the sandbox writes into a directory, derived from the FINAL url."""
    cases = [
        ("https://h/a/..%2f..%2fetc%2fpasswd", "traversal, percent-encoded"),
        ("https://h/..%2F..%2F.ssh%2Fauthorized_keys", "traversal, upper-case escapes"),
        ("https://h/x/%2e%2e%2f%2e%2e%2fshadow", "dot-segments, encoded"),
        ("https://h/a%00b.tsv", "an embedded NUL"),
        ("https://h/" + "n" * 4000 + ".tsv", "an absurdly long name"),
        ("https://h/", "no path at all"),
        ("https://h", "no path component"),
        ("https://h/....", "nothing but dots"),
        ("https://h/a%2Fb%5Cc.tsv", "both separators, encoded"),
        ("https://h/-rf%20*", "a name shaped like an argument"),
    ]
    for url, why in cases:
        name = fetch.filename_for(url)
        ok = (name
              and "/" not in name and "\\" not in name and "\x00" not in name
              and not name.startswith(".")
              and len(name) <= fetch.NAME_MAX
              and re.fullmatch(r"[A-Za-z0-9._-]+", name))
        check(f"filename_for is a plain name for {why}", bool(ok), f"{url!r} -> {name!r}")
    check("filename_for keeps an ordinary name intact",
          fetch.filename_for("https://h/gwas/summary.tsv.gz") == "summary.tsv.gz",
          fetch.filename_for("https://h/gwas/summary.tsv.gz"))
    check("filename_for falls back rather than returning an empty name",
          fetch.filename_for("https://h/") == "download", fetch.filename_for("https://h/"))


# =========================================================================================

def main():
    socket.create_connection = _recording_create_connection
    origin = start_origin()
    victim = start_origin()
    try:
        print("scheme and userinfo")
        test_scheme_and_userinfo()
        print("the metadata endpoint, by address and by name")
        test_metadata_endpoint()
        print("loopback and private literals")
        test_loopback_and_private_literals()
        print("a name that resolves private")
        test_name_resolving_to_rfc1918()
        print("DNS rebinding: public on the check, private on the connect")
        test_rebinding_is_pinned(victim)
        print("redirects")
        test_redirects(origin)
        print("the byte cap")
        test_byte_cap(origin)
        print("a body that ends early")
        test_truncated_body(origin)
        print("a Content-Encoding nobody asked for")
        test_content_encoding(origin)
        print("the header phase and the wall clock")
        test_header_phase_deadline(origin)
        print("the file name")
        test_filename_for()
        print("the wall clock")
        test_wall_clock(origin)
        print("check ordering, and what the loopback allowance unlocks")
        test_ordering_and_the_allowance(origin)
        print("the HTTP surface")
        test_surface(origin)
    finally:
        socket.create_connection = _real_create_connection
        origin.shutdown()
        origin.server_close()
        victim.shutdown()
        victim.server_close()

    print()
    non_loopback = sorted({h for h, _ in DIALLED
                           if not ipaddress.ip_address(h.strip("[]")).is_loopback})
    print(f"connect attempts to non-loopback addresses, all refused by the harness: "
          f"{non_loopback or 'none'}")
    if FAILURES:
        print(f"FAILED {len(FAILURES)}/{CHECKS} checks:")
        for line in FAILURES:
            print(f"  - {line}")
        return 1
    print(f"OK: {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
