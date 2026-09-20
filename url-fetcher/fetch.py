"""The fetch core: connect to the address the guard validated, and to no other.

WHY THIS IS NOT A CLIENT LIBRARY CALL. Every mainstream HTTP client resolves the hostname
again inside its own connect path, so the address that was checked and the address that is
dialled are two different lookups. A name that answers public during the check and private
during the connect — DNS rebinding — walks through an otherwise correct guard untouched. The
only fix is to open the socket at the validated address ourselves and hand TLS the hostname
separately, which `ssl.SSLContext.wrap_socket(..., server_hostname=...)` does directly and
`http.client` accepts as a pre-connected `sock`. That is the whole reason this file exists and
the reason the service has no third-party dependency.

Redirects are followed BY HAND for the same reason: each hop is a fresh URL and gets the whole
guard again, cap included.
"""

import base64
import hashlib
import http.client
import io
import os
import socket
import ssl
import time
import urllib.parse

from guard import Refused

# the vxtv.2 measurement: 512 KiB raw per input, because an input rides to the sandbox as
# base64 in a 1 MiB body that already carries up to 256 KiB of code. Nothing observed sits
# between 220 KB and 720 MB, so a larger cap buys an empty interval.
MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 5
TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10
CHUNK = 64 * 1024
# enough of an unexpected body to recognise an HTML error page as one, never enough to be
# mistaken for the file that was asked for
UPSTREAM_PREFIX_BYTES = 256
NAME_MAX = 128

USER_AGENT = "genetics-url-fetcher/1"


class Result:
    __slots__ = ("url", "name", "content", "content_type", "content_encoding", "sha256",
                 "redirects")

    def __init__(self, url, name, content, content_type, content_encoding, redirects):
        self.url = url
        self.name = name
        self.content = content
        self.content_type = content_type
        self.content_encoding = content_encoding
        self.sha256 = hashlib.sha256(content).hexdigest()
        self.redirects = redirects


def _header(response, name):
    """A header value with control characters stripped. It is reported as data inside a JSON
    body and is never re-emitted as a header, so this only has to stay printable."""
    value = response.getheader(name)
    if not value:
        return None
    return "".join(ch for ch in value if 32 <= ord(ch) < 127).strip()[:200] or None


def filename_for(url):
    """A plain file name from the FINAL url, sanitised down to characters that cannot mean
    anything to a path: the sandbox writes this name into a directory."""
    path = urllib.parse.urlsplit(url).path
    raw = urllib.parse.unquote(os.path.basename(path))
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in raw)
    safe = safe.lstrip(".")[:NAME_MAX]
    return safe or "download"


class _DeadlineSocket:
    """The connected socket with every read and write bounded by the WALL CLOCK.

    `socket.settimeout` is per-operation, and the header phase is parsed inside
    `http.client` before any code here regains control: an origin emitting one header line
    just inside the per-recv timeout resets the whole budget on every line, so the phase is
    bounded by (timeout x header count) rather than by the timeout. Re-arming from the
    deadline before each operation is what makes the budget total. It also bounds a body read
    that begins just under the deadline, which a check before the read cannot."""

    __slots__ = ("_sock", "_deadline")

    def __init__(self, sock, deadline):
        self._sock = sock
        self._deadline = deadline

    def __getattr__(self, name):
        return getattr(self._sock, name)

    def _arm(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("the wall clock ran out")
        self._sock.settimeout(remaining)

    def recv(self, *args, **kwargs):
        self._arm()
        return self._sock.recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._arm()
        return self._sock.recv_into(*args, **kwargs)

    def send(self, *args, **kwargs):
        self._arm()
        return self._sock.send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._arm()
        return self._sock.sendall(*args, **kwargs)

    def makefile(self, mode="rb", buffering=None, **_kwargs):
        # SocketIO reads through THIS object's recv_into, which is what puts http.client's
        # header parsing on the wall clock; socket.makefile would read the raw socket direct
        self._sock._io_refs += 1
        raw = socket.SocketIO(self, mode)
        if buffering in (None, -1):
            buffering = io.DEFAULT_BUFFER_SIZE
        return io.BufferedReader(raw, buffering)


def _connect(target, deadline):
    remaining = max(0.1, min(CONNECT_TIMEOUT_S, deadline - time.monotonic()))
    try:
        raw = socket.create_connection((str(target.address), target.port), timeout=remaining)
    except socket.timeout:
        # the ADDRESS never appears in a message that reaches the caller: naming it would make
        # this service a resolution oracle for allow-listed hosts and put an internal address
        # into a chat transcript. The POLICY is still named, everywhere it is what refused.
        raise Refused("timed_out", f"connecting to {target.host} timed out",
                      status=504, retryable=True) from None
    except OSError as exc:
        raise Refused("unreachable",
                      f"cannot connect to {target.host}: {exc.strerror or type(exc).__name__}",
                      status=502, retryable=True) from None

    if target.scheme != "https":
        conn = http.client.HTTPConnection(target.host, target.port)
        conn.sock = raw
        return conn

    context = ssl.create_default_context()
    try:
        # the pin: the socket is already at the validated address, and TLS is told the name
        # anyway so the certificate is still verified against the hostname
        tls = context.wrap_socket(raw, server_hostname=target.host)
    except socket.timeout:
        # before ssl.SSLError and OSError, both of which it is a subclass of: a handshake that
        # ran out of clock is the timeout taxonomy, not the unreachable one
        raw.close()
        raise Refused("timed_out", f"the TLS handshake with {target.host} timed out",
                      status=504, retryable=True) from None
    except ssl.SSLError as exc:
        raw.close()
        raise Refused("unreachable", f"TLS to {target.host} failed: {exc}",
                      status=502, retryable=False) from None
    except OSError as exc:
        raw.close()
        raise Refused("unreachable", f"TLS to {target.host} failed: {exc}",
                      status=502, retryable=True) from None
    conn = http.client.HTTPSConnection(target.host, target.port, context=context)
    conn.sock = tls
    return conn


def _read_capped(response, max_bytes, deadline, timeout_s, expected=None):
    """The body, or Refused the moment it passes the cap or ends early.

    Never truncated, in either direction: a short file that looks like a whole one is the
    worse failure, because the caller cannot tell it from the real thing. Over the cap the
    fetch is aborted; under a DECLARED length it is refused, because CPython's
    `HTTPResponse.read(amt)` answers a length-delimited body that was cut short with b""
    rather than IncompleteRead, so nothing else in this file would notice. `expected` is the
    declared length when the framing is length-delimited, and None when it is chunked — the
    chunked reader raises on its own."""
    chunks, total = [], 0
    while True:
        if time.monotonic() > deadline:
            raise Refused("timed_out", f"reading the body exceeded {timeout_s}s",
                          status=504, retryable=True)
        try:
            chunk = response.read(CHUNK)
        except socket.timeout:
            raise Refused("timed_out", f"reading the body exceeded {timeout_s}s",
                          status=504, retryable=True) from None
        except (http.client.HTTPException, OSError) as exc:
            raise Refused("unreachable", f"the connection failed mid-body: {exc}",
                          status=502, retryable=True) from None
        if not chunk:
            if expected is not None and total < expected:
                raise Refused("truncated",
                              f"the response ended after {total} bytes of the {expected} it "
                              "declared; a short body is refused rather than returned as a "
                              "complete file", status=502, retryable=True)
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise Refused("too_large",
                          f"the response exceeds the {max_bytes} byte cap and was aborted "
                          "rather than truncated", status=413, retryable=False)
        chunks.append(chunk)


class Fetcher:
    """Holds the guard and the caps. One instance serves every request; it keeps no state
    about who asked, because it is never told."""

    def __init__(self, guard, *, max_bytes=MAX_BYTES, max_redirects=MAX_REDIRECTS,
                 timeout_s=TIMEOUT_S):
        self.guard = guard
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout_s = timeout_s

    def fetch(self, url):
        deadline = time.monotonic() + self.timeout_s
        redirects = 0
        while True:
            target = self.guard.check(url)
            response, conn = self._request(target, deadline)
            try:
                status = response.status
                if status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise Refused("upstream_status",
                                      f"{target.host} answered {status} with no Location",
                                      status=502, retryable=False)
                    if redirects >= self.max_redirects:
                        raise Refused("refused_by_policy",
                                      f"more than {self.max_redirects} redirects")
                    redirects += 1
                    # resolved against the hop we are on, then re-checked in full at the top
                    url = urllib.parse.urljoin(url, location)
                    continue
                if not 200 <= status < 300:
                    self._raise_upstream(target, response, status)
                encoding = _header(response, "Content-Encoding")
                if encoding and encoding.lower() != "identity":
                    # the request asked for identity; an upstream is free to ignore that, and
                    # the only way to tell a transfer encoding from a file that happens to be
                    # compressed is to refuse the one we did not ask for. Relaying it would
                    # also carry the compression ratio past the cap.
                    raise Refused("unrequested_encoding",
                                  f"{target.host} answered with Content-Encoding "
                                  f"{encoding!r} although the request asked for identity",
                                  status=502, retryable=False)
                declared = response.getheader("Content-Length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise Refused("too_large",
                                  f"{target.host} declares {int(declared)} bytes, over the "
                                  f"{self.max_bytes} byte cap", status=413, retryable=False)
                expected = None
                if not getattr(response, "chunked", False) and declared and declared.isdigit():
                    expected = int(declared)
                body = _read_capped(response, self.max_bytes, deadline, self.timeout_s,
                                    expected=expected)
                return Result(url, filename_for(url), body,
                              _header(response, "Content-Type"), encoding, redirects)
            finally:
                conn.close()

    def _request(self, target, deadline):
        conn = _connect(target, deadline)
        conn.sock = _DeadlineSocket(conn.sock, deadline)
        try:
            # every header this service sends is written here. Nothing from the caller reaches
            # the upstream host: the request body carries a url and nothing else, and there is
            # no path by which a header could ride along.
            conn.request("GET", target.path, headers={
                "Host": target.host if target.port in (80, 443)
                        else f"{target.host}:{target.port}",
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                # identity, so that anything gzipped in the response is the FILE being
                # gzipped rather than a transfer encoding we would have to undo. The response
                # is checked against this above rather than trusted to honour it.
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = conn.getresponse()
        except socket.timeout:
            # the phase is named, because the deadline now fires in whichever phase spent it
            conn.close()
            raise Refused("timed_out", f"{target.host} did not send response headers within "
                                       f"{self.timeout_s}s", status=504, retryable=True) from None
        except (http.client.HTTPException, ssl.SSLError, OSError) as exc:
            conn.close()
            raise Refused("unreachable", f"{target.host} failed: {exc}",
                          status=502, retryable=True) from None
        return response, conn

    def _raise_upstream(self, target, response, status):
        """An upstream failure reported AS the bytes it was, never sniffed and never repaired.
        The first bytes travel with it so that an HTML error page served as a .tsv is
        recognisable as one by whoever reads the message."""
        try:
            prefix = response.read(UPSTREAM_PREFIX_BYTES)
        except (http.client.HTTPException, OSError, socket.timeout):
            prefix = b""
        content_type = _header(response, "Content-Type")
        raise Refused("upstream_status",
                      f"{target.host} answered {status} ({content_type or 'no content-type'})",
                      status=502,
                      retryable=status in (408, 425, 429, 500, 502, 503, 504),
                      details={
                          "upstream_status": status,
                          "upstream_content_type": content_type,
                          "upstream_body_prefix_b64":
                              base64.b64encode(prefix).decode("ascii"),
                      }) from None
