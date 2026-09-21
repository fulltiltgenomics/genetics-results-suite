"""The HTTP surface chat-backend calls, and what it deliberately is not.

TWO ROUTES AND NO THIRD: `GET /healthz` and `POST /fetch`.

UNAUTHENTICATED BY DESIGN, on the same standing as the sandbox supervisor's `/execute`: the
NetworkPolicy's ingress allow-list is the control, and this pod holds nothing worth stealing —
no service-account token, no workload identity, no secret. Adding a shared secret here would
put a credential in the one pod in the namespace that talks to the open internet, which is
the trade this service exists to avoid.

IT NEVER LEARNS WHO ASKED. The request body carries a url and nothing else: no user, no
session, no token, and nothing is logged that would identify a caller. That is why the
per-user fetch cache lives in chat-backend and not here.

The contract is stated in prose in docs/code-execution-security.md for the same reason the
sandbox's is: the two ends cannot import one module.
"""

import base64
import http.server
import json
import socketserver
import sys

from guard import Refused

MAX_REQUEST_BYTES = 8 * 1024
FETCH_PATH = "/fetch"
HEALTH_PATH = "/healthz"


def error_body(exc):
    body = {"type": exc.type, "message": exc.message, "retryable": bool(exc.retryable)}
    body.update(exc.details)
    return {"error": body}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # StreamRequestHandler puts this on the connection. Without it a caller that opens a
    # socket, declares a Content-Length and sends nothing holds a thread for as long as the
    # kernel keeps the connection, and ThreadingMixIn caps nothing.
    timeout = 30
    fetcher = None

    def version_string(self):
        return "genetics-url-fetcher"

    # -- plumbing ------------------------------------------------------------------------

    def _send(self, status, payload, body=True):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        # the only two headers this service emits. Nothing from the upstream response is
        # copied here, so no Set-Cookie, no redirect and no cache directive of theirs can
        # travel back to the caller; what is reported about the upstream rides in the body.
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if self.close_connection:
            # said out loud, not only acted on: a caller with a keep-alive pool that is not
            # told has to discover the close by failing a request on it
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(raw)

    def _fail(self, status, type, message, retryable=False):
        self._send(status, {"error": {"type": type, "message": message,
                                      "retryable": bool(retryable)}})

    def _refuse_unread(self, status, type, message, retryable=False):
        """Answer WITHOUT having read the request body, and close the connection.

        The undrained body is still in the stream. This handler speaks HTTP/1.1, so the
        connection stays open unless told otherwise, and any caller that reuses it — whether
        one does is not something this service can know or ought to depend on — would have its
        next request parsed out of those leftover bytes, so one caller's URL comes back as the
        answer to another's. Closing is the cheap half of
        the fix; the expensive half would be draining a body we have already refused."""
        self.close_connection = True
        self._fail(status, type, message, retryable)

    def send_error(self, code, message=None, explain=None):
        """Every failure leaves here as the documented envelope, including the ones this class
        defines no handler for. BaseHTTPRequestHandler's default is an HTML page, so HEAD,
        OPTIONS, TRACE and any unknown verb would hand a caller parsing {"error": {...}} a
        text/html 501 instead. The caller's own bytes are NOT echoed back: the default message
        embeds the request line, which is attacker-supplied."""
        self.close_connection = True
        reason = self.responses.get(code, ("error", ""))[0]
        self._send(code, {"error": {
            "type": "invalid_request",
            "message": f"{reason}; this service answers GET {HEALTH_PATH} and "
                       f"POST {FETCH_PATH} only",
            "retryable": False,
        }}, body=getattr(self, "command", None) != "HEAD" and code not in (204, 205, 304))

    def _read_body(self):
        """The url, or Refused. Every refusal raised BEFORE the body is read sets
        `close_connection`, because refusing without draining leaves the body framed into the
        next request on the connection."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise Refused("invalid_request", "unreadable Content-Length", status=400)
        if length <= 0:
            # includes a chunked request, whose framing this route does not read at all
            self.close_connection = True
            raise Refused("invalid_request", "empty request body", status=400)
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            raise Refused("invalid_request",
                          f"request body over {MAX_REQUEST_BYTES} bytes; this route takes a "
                          "url and nothing else", status=413)
        raw = self.rfile.read(length)
        if len(raw) != length:
            # the declared body never arrived in full, so the stream is out of frame
            self.close_connection = True
            raise Refused("invalid_request",
                          f"the request body ended after {len(raw)} of the {length} bytes it "
                          "declared", status=400)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Refused("invalid_request", f"body is not JSON: {exc}", status=400) from None
        if not isinstance(payload, dict):
            raise Refused("invalid_request", "body is not a JSON object", status=400)
        # an unknown field is refused rather than ignored: the body must carry nothing that
        # could steer the request, and silently dropping a field is how one arrives later
        extra = sorted(set(payload) - {"url"})
        if extra:
            raise Refused("invalid_request",
                          f"unknown field(s) {', '.join(extra)}; this route accepts 'url' "
                          "only", status=400)
        url = payload.get("url")
        if not isinstance(url, str) or not url.strip():
            raise Refused("invalid_request", "'url' must be a non-empty string", status=400)
        return url.strip()

    # -- routes --------------------------------------------------------------------------

    def do_GET(self):
        if self.path.split("?", 1)[0] == HEALTH_PATH:
            self._send(200, {"status": "ok"})
        else:
            self._refuse_unread(404, "invalid_request", f"no such route; {HEALTH_PATH} and "
                                                        f"POST {FETCH_PATH}")

    def do_POST(self):
        if self.path.split("?", 1)[0] != FETCH_PATH:
            self._refuse_unread(404, "invalid_request", f"no such route; {HEALTH_PATH} and "
                                                        f"POST {FETCH_PATH}")
            return
        try:
            url = self._read_body()
            result = self.fetcher.fetch(url)
        except Refused as exc:
            self._log(exc.type, getattr(exc, "message", ""))
            self._send(exc.status, error_body(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a surface that 500s silently is worse
            # NOT "unreachable" and NOT retryable: this is a bug in this service, and a caller
            # told the upstream failed retries something that cannot succeed. The exception
            # class is the whole disclosure — no message, no frames.
            self._log("internal_error", type(exc).__name__)
            self._fail(500, "internal_error",
                       f"the fetch failed inside the service: {type(exc).__name__}",
                       retryable=False)
            return
        self._log("ok", f"{result.name} {len(result.content)}B")
        self._send(200, {
            "url": result.url,
            "name": result.name,
            "size_bytes": len(result.content),
            "sha256": result.sha256,
            "content_type": result.content_type,
            "content_encoding": result.content_encoding,
            "redirects": result.redirects,
            "content_b64": base64.b64encode(result.content).decode("ascii"),
        })

    def do_PUT(self):
        self._refuse_unread(405, "invalid_request", "method not allowed")

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    # -- logging -------------------------------------------------------------------------

    def _log(self, outcome, detail):
        print(f"{self.command} {self.path} {outcome} {detail}", flush=True)

    def log_message(self, *args):
        """Silenced: the default line begins with the client address, and the one property
        this service promises is that it does not record who asked."""


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Overridden for the same reason Handler.log_message is silenced, and for one more.
        The default prints the CLIENT ADDRESS and a full traceback to stderr, which a caller
        disconnecting mid-write is enough to trigger; the address contradicts the property
        this service promises, and the frames are their own disclosure in a pod log."""
        print(f"url-fetcher: request failed: {sys.exc_info()[0].__name__}", flush=True)


def make_server(fetcher, host="0.0.0.0", port=8090):
    handler = type("BoundHandler", (Handler,), {"fetcher": fetcher})
    return Server((host, port), handler)


def serve(fetcher, host="0.0.0.0", port=8090):
    server = make_server(fetcher, host, port)
    print(f"url-fetcher listening on {host}:{port} "
          f"(allow-list: {', '.join(fetcher.guard.allowed_hosts)}; "
          f"loopback allowance: {fetcher.guard.allow_loopback_origin})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
