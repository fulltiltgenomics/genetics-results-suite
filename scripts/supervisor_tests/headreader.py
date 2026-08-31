import os
import re
import socket
import sys
import threading
import time

from .harness import _LogCapture, check, sup


ENV_DROP_LF_CRLF = "SUPERVISOR_TEST_DROP_LF_CRLF"


ENV_STATIC_SEAM = "SUPERVISOR_TEST_STATIC_SEAM"


def _static_seam(self, tail_len, take):
    """_read_head's seam update as it was before the rolling window: recomputed from THIS round
    only. The negative control for the drip checks below.

    With it installed a peek that returns fewer than _HEADER_TAIL_BYTES bytes discards what
    earlier rounds saw, so a blank line straddling it is never found: the read consumes the whole
    body off the kernel queue and blocks in recv_into forever. Measured over 4 terminator shapes
    x 7 chunk sizes: 26/28 with this installed, 28/28 without.
    """
    tail = min(take, sup._HEADER_TAIL_BYTES)
    self._edge[:tail] = self._view[take - tail:take]
    return tail


def _drip(sock, blob, chunk, delay):
    """Feed `blob` `chunk` bytes at a time on a thread, returned so the caller can join it.

    The join matters: reader.read() is a single recv by design, so a leftover assertion made
    while the feeder is still writing sees a short read and passes or fails for the wrong reason.
    """
    def go():
        try:
            for i in range(0, len(blob), chunk):
                sock.sendall(blob[i:i + chunk])
                time.sleep(delay)
        except OSError:
            pass  # the reader hung (the control is installed) and the socket was closed
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _drain(reader, n):
    out = b""
    while len(out) < n:
        block = reader.read(n - len(out))
        if not block:
            break
        out += block
    return out


def _read_head_watched(reader, timeout):
    """(result, heap_bytes_seen, still_running) for one _read_head call on its own thread.

    This is a mechanics test and makes no leak claim — a leak claim has to send from another
    process, because this process is the supervisor. What it asserts is narrower and checkable
    here: which objects _read_head puts a byte into. `heap_bytes_seen` is every `bytes` value
    that appeared as a local of the _read_head frame, so a body byte in one means a heap copy
    was built where a fixed, wiped buffer was required.
    """
    seen, box = [], []

    def tracer(frame, event, arg):
        if frame.f_code.co_name != "_read_head":
            return None
        for value in frame.f_locals.values():
            if type(value) is bytes:                                     # noqa: E721
                seen.append(value)
            elif type(value) is list:                                    # noqa: E721
                seen.extend(item for item in value if type(item) is bytes)  # noqa: E721
        return tracer

    def go():
        sys.settrace(tracer)
        try:
            box.append(reader._read_head())
        except BaseException as exc:                                     # noqa: BLE001
            box.append(exc)
        finally:
            sys.settrace(None)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return (box[0] if box else None), seen, t.is_alive()


def test_header_reader_units():
    """_HeaderBoundedReader's edges, which the wire tests cannot reach on purpose: a head split
    across segments, a head dripped ONE BYTE AT A TIME, a head that never terminates, a head
    whose blank line is "\\n\\r\\n", and both fixed buffers left clean."""
    pair = socket.socketpair
    saved_terminators = sup._HEADER_TERMINATORS
    saved_roll = sup._HeaderBoundedReader._roll_tail
    if os.environ.get(ENV_DROP_LF_CRLF) == "1":
        print(f"  !! {ENV_DROP_LF_CRLF}=1: the negative control is installed, "
              f"the \\n\\r\\n checks below MUST fail")
        sup._HEADER_TERMINATORS = tuple(t for t in sup._HEADER_TERMINATORS if t != b"\n\r\n")
    if os.environ.get(ENV_STATIC_SEAM) == "1":
        print(f"  !! {ENV_STATIC_SEAM}=1: the negative control is installed, "
              f"the drip checks below MUST fail")
        sup._HeaderBoundedReader._roll_tail = _static_seam
    try:
        _header_reader_cases(pair)
    finally:
        sup._HEADER_TERMINATORS = saved_terminators
        sup._HeaderBoundedReader._roll_tail = saved_roll


def _header_reader_cases(pair):

    # 1. head and body in one write: the head comes back exactly, the body stays in the kernel.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    a.sendall(b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n\r\nBODY!")
    line = reader.readline(65537)
    check("header reader: the request line is returned",
          line == b"POST /x HTTP/1.1\r\n", f"got {line!r}")
    rest = b"".join(iter(lambda: reader.readline(65537), b"\r\n"))
    check("header reader: the headers are returned and stop at the blank line",
          rest == b"Host: h\r\nContent-Length: 5\r\n", f"got {rest!r}")
    check("header reader: the body is still readable afterwards",
          reader.read(5) == b"BODY!")
    check("header reader: the peek scratch is zeroed, so no body byte is left in it",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES))
    a.close(); b.close()

    # 2. a head split across three segments, with the terminator itself bisected — and a body
    # riding in the last segment, so the seam search sees body bytes and must not copy them.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    whole = b"GET /y HTTP/1.1\r\nA: " + b"z" * 900 + b"\r\n\r\n"
    def _feed():
        a.sendall(whole[:10]); time.sleep(0.05)
        a.sendall(whole[10:len(whole) - 2]); time.sleep(0.05)
        a.sendall(whole[len(whole) - 2:] + b"\x01\x02BODYBODY")
    threading.Thread(target=_feed, daemon=True).start()
    head, heap, alive = _read_head_watched(reader, 10)
    check("header reader: a head split across segments, terminator bisected, is reassembled",
          not alive and head == whole, f"got {head!r} (still running: {alive})")
    # The seam window straddles the boundary, so its trailing bytes ARE body bytes. Building it
    # as `tail + bytes(view[:n])` put them in a heap `bytes` freed into an arena the fork server
    # would snapshot. The needle is one byte on purpose — the pre-fix copy carried only 1 or 2
    # body bytes here, so a word-sized needle would pass against the defect it exists to catch.
    escaped = [chunk for chunk in heap if b"\x01" in chunk or b"\x02" in chunk]
    check("header reader: the bisected-terminator path copied NO body byte onto the heap",
          not escaped, f"heap bytes carrying body: {escaped!r}")
    check("header reader: both fixed buffers are zeroed after the seam read",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
          and reader._edge == bytearray(sup._HEADER_EDGE_BYTES),
          f"scratch clean: {reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)}, "
          f"edge: {bytes(reader._edge)!r}")
    a.close(); b.close()

    # 2b. The blank line is "\n\r\n" — the fourth shape http.client.parse_headers stops on, and
    # the one a three-member terminator set misses. Not reachable from the deployed path, but
    # with it missing this was measured to copy the whole body into `parts` on the heap, leave it
    # in the un-wiped scratch, and block in recv_into forever instead of refusing. Run with
    # SUPERVISOR_TEST_DROP_LF_CRLF=1 to drop it from the set and watch these go red.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    lf_head = b"POST /execute HTTP/1.1\r\nHost: h\nContent-Length: 15\n\r\n"
    a.sendall(lf_head + b"SECRETBODYNEEDLE"[:15])
    head, heap, alive = _read_head_watched(reader, 5)
    check("header reader: a head whose blank line is \\n\\r\\n terminates and is consumed exactly",
          not alive and head == lf_head, f"got {head!r} (still blocked in recv_into: {alive})")
    check("header reader: that head's body is not copied onto the heap",
          not [chunk for chunk in heap if b"SECRETBODY" in chunk],
          f"heap bytes carrying body: {[c for c in heap if b'SECRETBODY' in c]!r}")
    check("header reader: that head's body is not left in the scratch buffer",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES),
          f"scratch: {bytes(reader._scratch)[:80]!r}")
    if not alive:
        check("header reader: that head's body is still in the kernel queue afterwards",
              reader.read(15) == b"SECRETBODYNEEDL")
    a.close(); b.close()

    # 2c. The same failure reached through peek size rather than terminator shape: an ordinary,
    # entirely valid \r\n\r\n head delivered a byte at a time. The seam window used to be
    # recomputed from the current round, so a 1-byte peek shrank it to 1 byte and the straddling
    # blank line was never found — the whole body was consumed off the kernel queue into `parts`
    # and the read blocked forever instead of refusing. `total` never approaches MAX_HEADER_BYTES
    # on that path either, so _HeaderTooLarge never fires: fail-open and pre-auth. Run with
    # SUPERVISOR_TEST_STATIC_SEAM=1 to put the per-round window back.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    drip_head = b"POST /execute HTTP/1.1\r\nHost: h\r\nContent-Length: 20\r\n\r\n"
    # The body opens with two one-byte needles, which is what makes the heap check bite here: at
    # a byte a drip `parts` fills with 1-byte `bytes`, so a word-sized needle could never appear
    # in one. \x01 and \x02 cannot occur in a head and are in no terminator.
    drip_body = b"\x01\x02SECRETBODYNEEDL" + b"AA"
    feeder = _drip(a, drip_head + drip_body, 1, 0.004)
    head, heap, alive = _read_head_watched(reader, 20)
    feeder.join(20)
    check("header reader: a head dripped one byte at a time terminates and is consumed exactly",
          not alive and head == drip_head,
          f"got {head!r} (still blocked in recv_into: {alive})")
    leaked = [c for c in heap if b"\x01" in c or b"\x02" in c or b"SECRETBODY" in c]
    check("header reader: the dripped head's body is not copied onto the heap",
          not leaked, f"{len(leaked)} heap bytes carrying body, e.g. {leaked[:6]!r}")
    check("header reader: the dripped head's body is still in the kernel queue afterwards",
          not alive and _drain(reader, len(drip_body)) == drip_body)
    check("header reader: both fixed buffers are zeroed after the dripped read",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
          and reader._edge == bytearray(sup._HEADER_EDGE_BYTES),
          f"scratch clean: {reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)}, "
          f"edge: {bytes(reader._edge)!r}")
    a.close(); b.close()

    # 2d. And the whole matrix, so the take == 1 class is asserted rather than spot-checked:
    # every blank-line shape http.client.parse_headers stops on, crossed with chunk sizes that
    # make a peek land inside the terminator. 28/28 here, 26/28 with SUPERVISOR_TEST_STATIC_SEAM
    # =1, and the two that fail there are exactly the take == 1 cells whose blank line is \r\n —
    # which is why one-write clients never saw this and any peer that can connect can trigger it.
    for prev in (b"\r\n", b"\n"):
        for blank in (b"\r\n", b"\n"):
            bad = []
            for chunk in (1, 2, 3, 4, 5, 7, 512):
                m_head = (b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 20"
                          + prev + blank)
                m_body = b"M" * 20
                a, b = pair()
                reader = sup._HeaderBoundedReader(b, None)
                feeder = _drip(a, m_head + m_body, chunk, 0.004 if chunk <= 2 else 0.002)
                head, _, alive = _read_head_watched(reader, 20)
                feeder.join(20)
                left = b"" if alive else _drain(reader, len(m_body))
                if not (not alive and head == m_head and left == m_body
                        and reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
                        and reader._edge == bytearray(sup._HEADER_EDGE_BYTES)):
                    bad.append(f"chunk={chunk} hung={alive} head={head!r} left={left!r}")
                a.close(); b.close()
            check(f"header reader: a head ending {prev + blank!r} is consumed exactly and "
                  f"leaves its body in the kernel at every chunk size", not bad,
                  "; ".join(bad))

    # 3. a head that never terminates fails CLOSED at MAX_HEADER_BYTES.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    box = []
    def _over():
        try:
            reader.readline(65537)
        except sup._HeaderTooLarge:
            box.append("refused")
        except Exception as exc:                       # noqa: BLE001
            box.append(repr(exc))
    t = threading.Thread(target=_over, daemon=True)
    t.start()
    try:
        sent = 0
        while sent <= sup.MAX_HEADER_BYTES + sup.HEADER_PEEK_BYTES and t.is_alive():
            a.sendall(b"X: " + b"q" * 1021 + b"\r\n")
            sent += 1024
        t.join(10)
    finally:
        a.close(); b.close()
    check("header reader: a head that never terminates is refused, not buffered forever",
          box == ["refused"], f"got {box!r}")
    # The wipe is in a `finally`, so it has to survive the raise as well as the return — the
    # refusal path is the one where a caller has most reason to assume nothing was kept.
    check("header reader: both fixed buffers are zeroed on the _HeaderTooLarge path too",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
          and reader._edge == bytearray(sup._HEADER_EDGE_BYTES),
          f"scratch clean: {reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)}, "
          f"edge: {bytes(reader._edge)!r}")

    # 4. and over the wire that refusal has to be a real answer, not a traceback in the log:
    # _HeaderTooLarge escapes handle_one_request, which is a path socketserver would otherwise
    # turn into a dropped connection with no response.
    httpd = sup._Server(("127.0.0.1", 0), sup._Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        wire = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=20)
        wire.sendall(b"GET /health HTTP/1.1\r\nHost: h\r\n")
        sent = 0
        try:
            while sent < sup.MAX_HEADER_BYTES + 4096:
                wire.sendall(b"X: " + b"q" * 1021 + b"\r\n")
                sent += 1024
        except OSError:
            pass  # the supervisor answered and closed while we were still writing
        wire.settimeout(20)
        answer = b""
        try:
            while True:
                block = wire.recv(4096)
                if not block:
                    break
                answer += block
        except OSError:
            pass
        wire.close()
        check("header reader: an over-long head is answered 431 in the uniform JSON shape and "
              "the connection is closed",
              answer.startswith(b"HTTP/1.1 431 ") and b'"PayloadTooLarge"' in answer,
              f"got {answer[:160]!r}")
    finally:
        httpd.shutdown()
        httpd.server_close()


ENV_ARM_ONCE = "SUPERVISOR_TEST_ARM_ONCE"


def _arm_once(self, deadline):
    """_read_head's timeout as it would be if it were armed ONCE PER CONNECTION — in setup(),
    say — instead of once per head. The negative control for the keep-alive check below.

    The second head on a kept-alive connection then reads with whatever _read_body's finally left
    on the socket, which is settimeout(None): no deadline at all. The first-request check still
    passes, which is exactly what makes the defect shape "works once, then silently stops".
    """
    if getattr(self, "_armed_once", False):
        return
    self._armed_once = True
    _real_arm(self, deadline)


_real_arm = sup._HeaderBoundedReader._arm


ENV_IDLE_FOREVER = "SUPERVISOR_TEST_IDLE_FOREVER"


ENV_PER_RECV = "SUPERVISOR_TEST_PER_RECV_TIMEOUT"


def _arm_no_idle(self, deadline):
    """_arm as it would be if only a STARTED head were bounded and silence were not.

    The negative control for the idle-close check: the handler thread parks in recv_into forever
    and the suite stays silent, because a connection that is never answered and one that is
    legitimately quiet look identical to any check that only asserts "nothing was written". Only
    observing the CLOSE tells them apart.
    """
    if deadline is None:
        self._sock.settimeout(None)
        return
    _real_arm(self, deadline)


def _arm_per_recv(self, deadline):
    """_arm as it would be if the head bound were a PER-RECV timer instead of one deadline.

    The negative control for the total-exceeds-budget drip below. Every round gets a fresh
    HEAD_READ_TIMEOUT_S, so a peer sending one byte just inside the timer resets it forever.
    Every existing head check still passes with this installed, which is why one that fails is
    needed.
    """
    if deadline is None:
        self._sock.settimeout(sup.IDLE_READ_TIMEOUT_S)
        return
    self._sock.settimeout(sup.HEAD_READ_TIMEOUT_S)


def _await_eof(wire, timeout):
    """(bytes read, whether the PEER closed, seconds waited).

    _slurp returns b"" both when the server closed without writing and when the harness's own
    socket timeout expired, and for the idle bound that distinction IS the property: an idle
    connection held forever is also silent.
    """
    wire.settimeout(timeout)
    began = time.monotonic()
    out = b""
    closed = False
    try:
        while True:
            block = wire.recv(4096)
            if not block:
                closed = True
                break
            out += block
    except OSError:
        pass
    return out, closed, time.monotonic() - began


def _raw_wire(server, timeout=40):
    return socket.create_connection((server.host, server.port), timeout=timeout)


def _slurp(wire, timeout=40):
    """Everything the supervisor writes until it closes, or b"" if it never answers."""
    wire.settimeout(timeout)
    out = b""
    try:
        while True:
            block = wire.recv(4096)
            if not block:
                break
            out += block
    except OSError:
        pass
    return out


def _one_response(wire, timeout):
    """One complete response off a KEPT-ALIVE connection, which cannot be read to EOF."""
    wire.settimeout(timeout)
    out = b""
    try:
        while b"\r\n\r\n" not in out:
            block = wire.recv(4096)
            if not block:
                break
            out += block
        head, _, body = out.partition(b"\r\n\r\n")
        match = re.search(rb"Content-Length: (\d+)", head)
        want = int(match.group(1)) if match else 0
        while len(body) < want:
            block = wire.recv(4096)
            if not block:
                break
            body += block
    except OSError:
        pass
    return out


def test_head_timeout(server):
    """The head read has the deadline BODY_READ_TIMEOUT_S's comment used to claim.

    The measured defect was a connection that sent a single b"P" and was still open at 35s: the
    head is read before _execute takes `started`, so nothing bounded it. These checks are on the
    wire on purpose — the claim is about what a peer holding a socket can do.
    """
    # 1. THE MEASURED CASE. A head that starts and stops is answered, not held.
    wire = _raw_wire(server)
    wire.sendall(b"P")
    began = time.monotonic()
    answer = _slurp(wire, sup.HEAD_READ_TIMEOUT_S + 20)
    elapsed = time.monotonic() - began
    wire.close()
    check("head timeout: a head that starts and stalls is answered 408 in the uniform JSON "
          "shape and the connection is closed",
          answer.startswith(b"HTTP/1.1 408 ") and b'"RequestTimeout"' in answer
          and b'"execution_id": null' in answer,
          f"got {answer[:160]!r} after {elapsed:.1f}s")
    check("head timeout: it waits for the deadline rather than refusing a slow client outright",
          sup.HEAD_READ_TIMEOUT_S * 0.5 <= elapsed <= sup.HEAD_READ_TIMEOUT_S + 15,
          f"answered after {elapsed:.1f}s, deadline is {sup.HEAD_READ_TIMEOUT_S}s")

    # 2. A CONNECTION THAT SENDS NOTHING AT ALL is a different case and must not be answered
    # 408 after HEAD_READ_TIMEOUT_S: that is every kept-alive client between requests, the
    # kubelet's readiness probe included. It is bounded by IDLE_READ_TIMEOUT_S instead.
    check("head timeout: the idle bound is far longer than the head bound, so a kept-alive "
          "client is not the thing being timed",
          sup.IDLE_READ_TIMEOUT_S >= 4 * sup.HEAD_READ_TIMEOUT_S,
          f"idle {sup.IDLE_READ_TIMEOUT_S}s vs head {sup.HEAD_READ_TIMEOUT_S}s")
    wire = _raw_wire(server)
    quiet = _slurp(wire, sup.HEAD_READ_TIMEOUT_S + 3)
    wire.close()
    check("head timeout: a connection that has sent NOTHING is not answered 408 at the head "
          "deadline", quiet == b"", f"got {quiet[:120]!r}")

    # ...and it is nonetheless CLOSED, which the two checks above cannot see: one asserts only
    # that nothing was written, which a connection held forever also satisfies, and the other
    # reads the constants rather than the wire. Neutering the idle branch to settimeout(None)
    # left both green while a zero-byte connection pinned a daemon handler thread indefinitely.
    # The constant is dropped to 3s because the property is that the close happens on the idle
    # bound, not what the bound is.
    idle_installed = os.environ.get(ENV_IDLE_FOREVER) == "1"
    if idle_installed:
        sup._HeaderBoundedReader._arm = _arm_no_idle
    real_idle = sup.IDLE_READ_TIMEOUT_S
    sup.IDLE_READ_TIMEOUT_S = 3.0
    try:
        wire = _raw_wire(server)
        held, closed, waited = _await_eof(wire, 3.0 * 4)
        wire.close()
    finally:
        sup.IDLE_READ_TIMEOUT_S = real_idle
        sup._HeaderBoundedReader._arm = _real_arm
    check("head timeout: a connection that sends NOTHING is CLOSED at roughly "
          "IDLE_READ_TIMEOUT_S — silence is the response, not the outcome",
          closed and held == b"" and 3.0 * 0.5 <= waited <= 3.0 + 6.0,
          f"closed={closed} after {waited:.1f}s with {held[:80]!r}, bound was 3.0s"
          + (" (SUPERVISOR_TEST_IDLE_FOREVER=1 is installed: this is the control)"
             if idle_installed else ""))

    # 3. A SLOW BUT HEALTHY CLIENT — /health dripped a byte at a time, well inside the budget —
    # still gets its 200. The readiness probe must not be able to fail on a slow write.
    wire = _raw_wire(server)
    head = b"GET /health HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n"
    feeder = _drip(wire, head, 1, 0.005)
    answer = _slurp(wire, sup.HEAD_READ_TIMEOUT_S + 20)
    feeder.join(20)
    wire.close()
    check("head timeout: a /health head dripped a byte at a time still answers 200",
          answer.startswith(b"HTTP/1.1 200 "), f"got {answer[:120]!r}")

    # ...and a drip whose per-byte gap is inside the budget but whose TOTAL is not. This is the
    # check that tells one absolute deadline from a per-recv timer, and the drip above cannot:
    # 47 bytes at 5ms completes under either design. At 0.4s per byte against a 2s head bound the
    # deadline answers 408 six bytes in, while a re-armed timer would answer 200 after ~19s.
    recv_installed = os.environ.get(ENV_PER_RECV) == "1"
    if recv_installed:
        sup._HeaderBoundedReader._arm = _arm_per_recv
    real_head = sup.HEAD_READ_TIMEOUT_S
    sup.HEAD_READ_TIMEOUT_S = 2.0
    try:
        wire = _raw_wire(server)
        feeder = _drip(wire, head, 1, 0.4)
        began = time.monotonic()
        answer = _slurp(wire, 2.0 + 30)
        elapsed = time.monotonic() - began
        wire.close()
        feeder.join(30)
    finally:
        sup.HEAD_READ_TIMEOUT_S = real_head
        sup._HeaderBoundedReader._arm = _real_arm
    check("head timeout: a drip inside the per-byte budget but over the TOTAL is answered 408 "
          "— the head has ONE deadline, not a timer each recv resets",
          answer.startswith(b"HTTP/1.1 408 ") and b'"RequestTimeout"' in answer,
          f"got {answer[:120]!r} after {elapsed:.1f}s against a 2.0s head bound"
          + (" (SUPERVISOR_TEST_PER_RECV_TIMEOUT=1 is installed: this is the control)"
             if recv_installed else ""))

    # 4. The keep-alive case, which is why this is not a one-liner: _read_body's finally does
    # settimeout(None), so a deadline armed once per connection is gone by the second request.
    # The first request below reaches _read_body, so the disarm has definitely run.
    installed = os.environ.get(ENV_ARM_ONCE) == "1"
    if installed:
        sup._HeaderBoundedReader._arm = _arm_once
    try:
        wire = _raw_wire(server)
        body = b'{"code": 1}'
        wire.sendall(b"POST /execute HTTP/1.1\r\nHost: h\r\n"
                     b"Content-Type: application/json\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        first = _one_response(wire, 40)
        check("head timeout: the first request on the connection is read to the end of its body "
              "and kept alive", first.startswith(b"HTTP/1.1 400 ") and b"Connection: close" not in first,
              f"got {first[:160]!r}")
        wire.sendall(b"G")
        began = time.monotonic()
        answer = _slurp(wire, sup.HEAD_READ_TIMEOUT_S + 20)
        elapsed = time.monotonic() - began
        wire.close()
        check("head timeout: the SECOND head on a kept-alive connection is bounded too — the "
              "deadline is armed per head, not per connection",
              answer.startswith(b"HTTP/1.1 408 ") and b'"RequestTimeout"' in answer,
              f"got {answer[:160]!r} after {elapsed:.1f}s"
              + (" (SUPERVISOR_TEST_ARM_ONCE=1 is installed: this is the control)"
                 if installed else ""))
    finally:
        sup._HeaderBoundedReader._arm = _real_arm

    # 5. The request line reaches LOG.info raw, and since the audit stream landed
    # that stream IS the audit channel. An ESC in the path must not reach it as an ESC.
    with _LogCapture() as capture:
        wire = _raw_wire(server)
        wire.sendall(b"GET /\x1b[31mnope HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n")
        answer = _slurp(wire, 30)
        wire.close()
    logged = [line for line in capture.lines if "nope" in line]
    check("log sanitising: a request line with an ESC is still routed normally (404)",
          answer.startswith(b"HTTP/1.1 404 "), f"got {answer[:120]!r}")
    check("log sanitising: and the ESC reaches the audit stream escaped, not raw",
          logged and all("\x1b" not in line for line in logged)
          and any("\\x1b" in line for line in logged),
          f"logged {logged!r}")

    # Directly, because the wire cannot deliver every control character in a routable request
    # line: a bare CR or LF would end the request line itself. These are the characters that
    # make a `kubectl logs` session lie about how many records there are.
    handler = sup._Handler.__new__(sup._Handler)
    handler.client_address = ("127.0.0.1", 4321)
    with _LogCapture() as capture:
        handler.log_message('"%s" %s %s', "GET /a\r\nfake HTTP/1.1", "200", "-")
    check("log sanitising: CR and LF in a logged request line are escaped, so one request "
          "cannot become two log lines",
          len(capture.lines) == 1 and not any(ord(c) < 0x20 for c in capture.lines[0])
          and "\\x0d\\x0a" in capture.lines[0],
          f"logged {capture.lines!r}")
