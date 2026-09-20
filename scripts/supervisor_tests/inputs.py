"""The `inputs` channel: what /execute accepts, what reaches the child, and what is left after.

The validation half is the cheap half. The interesting failures are in delivery and lifecycle —
bytes that arrive altered, a directory that outlives its execution, one execution's file visible
to the next — so every one of those is driven end to end through a real fork and then driven
again with the control removed.

Three controls restore a defect rather than reasoning about one:

* a read-only seal put BACK onto inputs/ in _deliver_inputs, built from the real source. Every
  route that removes an execution directory — _retain, _forget_retained, the orphan sweep and
  the startup wipe — is an rmtree, and rmtree cannot unlink out of a 0500 directory, so the
  seal is what an ordinary directory buys its way out of. It is driven twice: on the crash path,
  where only the startup wipe is left, and across a real execution, where _retain fails.
* the digest comparison in _parse_inputs, cut the same way. It is the only thing between a
  corrupted fetch and an analysis run on the wrong bytes.

Each cut is anchored in the supervisor's own source and the run EXITS 2 if an anchor stops
matching, because a control that quietly stops being built is worse than no control: the probe
next to it goes on passing and nothing says the run proved less than it printed.

MODES ARE NOT A BOUNDARY HERE and nothing below implies they are. The child shares the
supervisor's uid, so it owns the files and can chmod them. What 0400 buys is that a script which
meant to write next to its input gets an error instead of overwriting the bytes it is analysing
— so the checks assert exactly that pair: overwriting a delivered file is refused, and the same
write after a chmod succeeds. The directory is an ordinary 0700, so writing a NEW name into it
succeeds and the checks say so.
"""

import ast
import base64
import hashlib
import json
import os
import shutil
import socket
import sys
import types

from .harness import (
    ROOT,
    Server,
    _LogCapture,
    check,
    expect_request_error,
    make_body,
    skip,
    sup,
)

SUPERVISOR_PY = os.path.join(ROOT, "sandbox", "supervisor.py")


def _harness_error(message):
    """Exit 2: the harness could not run this group, as distinct from a property being broken.

    Reached only when a cut's anchor no longer matches the source. Passing quietly there would
    leave the positive controls silently unbuilt.
    """
    print(f"HARNESS: {message}", file=sys.stderr)
    raise SystemExit(2)


# Each entry: the supervisor function to rebuild, and the exact lines to remove from it. The
# anchors are the guard itself, not its neighbourhood, so a rename fails loudly rather than
# cutting something adjacent.
_CUTS = {
    # Not a cut but a restoration: it puts back the 0500 seal inputs/ used to carry, so the
    # cost of that seal is measurable rather than argued. The anchor is the write loop's own
    # close, and the replacement dedents to the function body, which is where the chmod went.
    "seal-the-directory": ("_deliver_inputs", [
        ("            os.close(fd)",
         "            os.close(fd)\n    os.chmod(job.dirs.inputs, 0o500)"),
    ]),
    "no-digest-check": ("_parse_inputs", [
        ("        if hashlib.sha256(data).hexdigest() != digest:", "        if False:"),
    ]),
}


def _cut(name):
    """Rebuild one supervisor function from the real source, with its guard removed or the
    hazard it replaced put back.

    Source rather than a hand-written stand-in: a control written by hand drifts from the thing
    it is controlling for, and then proves that the harness's copy is broken rather than that
    the supervisor's guard is load-bearing.
    """
    fn_name, table = _CUTS[name]
    with open(SUPERVISOR_PY, encoding="utf-8") as handle:
        source = handle.read()
    node = next((n for n in ast.parse(source).body
                 if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    if node is None:
        _harness_error(f"the cut {name!r} names {fn_name}(), which is no longer a module-level "
                       f"function in sandbox/supervisor.py. This run proves nothing — fix the "
                       f"anchor.")
    segment = ast.get_source_segment(source, node) or ""
    for anchor, replacement in table:
        if segment.count(anchor) != 1:
            _harness_error(
                f"the cut {name!r} no longer matches {fn_name}() "
                f"({segment.count(anchor)} occurrences of its anchor). The positive control "
                f"cannot be built, so this run proves nothing — fix the anchor.")
        segment = segment.replace(anchor, replacement)
    namespace = dict(sup.__dict__)
    try:
        exec(compile(segment, f"{SUPERVISOR_PY} [cut: {name}]", "exec"), namespace)
    except Exception as exc:
        _harness_error(f"the mutant {fn_name}() [cut: {name}] does not compile: {exc}")
    return namespace[fn_name]


def _element(name, data, digest=None, **extra):
    element = {
        "name": name,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest() if digest is None else digest,
    }
    element.update(extra)
    return element


def _parse(**kw):
    return lambda: sup.parse_execute_request(json.dumps(make_body(**kw)).encode())


def _report(body):
    for line in (body or {}).get("output", "").splitlines():
        if line.startswith("REPORT:"):
            return json.loads(line[len("REPORT:"):])
    return None


# Reads and reports BEFORE it mutates anything, so the digests below are the bytes as delivered.
_PROBE = r'''
import hashlib, json, os, stat
d = os.environ.get("SANDBOX_INPUTS_DIR")
r = {"dir": d, "env_set": d is not None, "files": {}, "peer": %(peer)r,
     "peer_exists": os.path.exists(%(peer)r) if %(peer)r else None}
r["names"] = sorted(os.listdir(d))
r["dir_mode"] = stat.S_IMODE(os.stat(d).st_mode)
for name in r["names"]:
    p = os.path.join(d, name)
    with open(p, "rb") as fh:
        blob = fh.read()
    r["files"][name] = {"mode": stat.S_IMODE(os.stat(p).st_mode), "size": len(blob),
                        "sha256": hashlib.sha256(blob).hexdigest()}
# the accident guard, asserted as what it is: the same uid owns all of this, so the only real
# property is "overwriting a delivered file fails, a deliberate chmod then succeeds". The
# directory is ordinary, so a new name goes in without complaint.
try:
    open(os.path.join(d, "a-new-name"), "w").write("x")
    r["create_refused"] = False
except OSError as exc:
    r["create_refused"] = type(exc).__name__
if r["names"]:
    victim = os.path.join(d, r["names"][0])
    try:
        open(victim, "wb").write(b"clobbered")
        r["overwrite_refused"] = False
    except OSError as exc:
        r["overwrite_refused"] = type(exc).__name__
    try:
        os.chmod(victim, 0o600)
        open(victim, "wb").write(b"clobbered")
        r["overwrite_after_chmod"] = True
    except OSError as exc:
        r["overwrite_after_chmod"] = type(exc).__name__
%(extra)s
print("REPORT:" + json.dumps(r))
'''


def _probe(peer=None, extra=""):
    return _PROBE % {"peer": peer or "", "extra": extra}


def test_inputs_parsing():
    """Every rejection the contract names, each with its status and its error type."""
    payload = b"rsid\tbeta\nrs1\t0.1\n"
    good = _element("covid.tsv", payload, content_type="text/tab-separated-values")

    req = sup.parse_execute_request(json.dumps(make_body()).encode())
    check("inputs: absent is an empty tuple, not None", req.inputs == (), f"got {req.inputs!r}")
    req = sup.parse_execute_request(json.dumps(make_body(inputs=[])).encode())
    check("inputs: an empty array parses", req.inputs == (), f"got {req.inputs!r}")

    req = sup.parse_execute_request(json.dumps(make_body(inputs=[good])).encode())
    check("inputs: a valid element keeps name, bytes and content_type",
          len(req.inputs) == 1 and req.inputs[0].name == "covid.tsv"
          and req.inputs[0].data == payload
          and req.inputs[0].content_type == "text/tab-separated-values",
          f"got {req.inputs!r}")
    check("inputs: the digest is not retained — it can only speak at parse time",
          "sha256" not in sup.InputFile.__slots__, f"got {sup.InputFile.__slots__}")
    req = sup.parse_execute_request(
        json.dumps(make_body(inputs=[_element("a.txt", b"x")])).encode())
    check("inputs: content_type is optional and defaults to None",
          req.inputs[0].content_type is None, f"got {req.inputs[0].content_type!r}")

    # Derived from the module's own field set rather than a list written here, so adding a
    # sub-field to the contract cannot leave this check quietly testing the old one.
    for unknown in ("path", "url", "sha1", "content"):
        if unknown in sup._INPUT_FIELDS:
            continue
        expect_request_error(
            f"inputs: unknown sub-field {unknown!r} -> 400 UnknownField",
            _parse(inputs=[dict(good, **{unknown: "x"})]), 400, "UnknownField")

    for label, value in (("a string", "nope"), ("an object", {"a": 1}), ("a number", 7),
                         ("a bare element", good)):
        expect_request_error(f"inputs: {label} instead of an array -> 400",
                             _parse(inputs=value), 400, "InvalidRequest")
    for label, value in (("a string", "nope"), ("null", None), ("a list", [1])):
        expect_request_error(f"inputs: element is {label}, not an object -> 400",
                             _parse(inputs=[value]), 400, "InvalidRequest")

    for label, name in (
        ("a path separator", "sub/dir.tsv"),
        ("a backslash", "sub\\dir.tsv"),
        ("dot-dot", ".."),
        ("a traversal", "../etc/passwd"),
        ("a leading dot", ".hidden"),
        ("a leading dash", "-rf"),
        ("empty", ""),
        ("a space", "two words.tsv"),
        ("a newline", "name\ninjected"),
        ("a NUL", "name\x00.tsv"),
        ("65 characters", "a" * 65),
        ("a non-string", 7),
        ("null", None),
    ):
        expect_request_error(f"inputs: a name with {label} -> 400",
                             _parse(inputs=[_element(name, payload)]) if isinstance(name, str)
                             else _parse(inputs=[dict(good, name=name)]),
                             400, "InvalidRequest")
    check("inputs: 64 characters is the boundary and is accepted",
          sup.parse_execute_request(json.dumps(
              make_body(inputs=[_element("a" * 64, payload)])).encode()).inputs[0].name
          == "a" * 64)

    expect_request_error(
        "inputs: two elements one name -> 400, never a merge",
        _parse(inputs=[_element("same.tsv", b"first"), _element("same.tsv", b"second")]),
        400, "InvalidRequest")

    for label, element in (
        ("missing", {k: v for k, v in good.items() if k != "content_base64"}),
        ("a non-string", dict(good, content_base64=7)),
        ("not base64", dict(good, content_base64="!!!not base64!!!")),
        ("base64 with an embedded newline", dict(good, content_base64="aGVs\nbG8=")),
        ("base64 with bad padding", dict(good, content_base64="aGVsbG8")),
    ):
        expect_request_error(f"inputs: content_base64 {label} -> 400",
                             _parse(inputs=[element]), 400, "InvalidRequest")

    for label, element in (
        ("missing", {k: v for k, v in good.items() if k != "sha256"}),
        ("a non-string", dict(good, sha256=7)),
        ("63 hex digits", dict(good, sha256="a" * 63)),
        ("uppercase hex", dict(good, sha256=hashlib.sha256(payload).hexdigest().upper())),
        ("not hex at all", dict(good, sha256="z" * 64)),
    ):
        expect_request_error(f"inputs: sha256 {label} -> 400",
                             _parse(inputs=[element]), 400, "InvalidRequest")

    # The one check about the BYTES rather than the shape, and the only thing standing between a
    # corrupted fetch and an analysis. Driven three ways a truncated or spliced transfer
    # actually looks, not once with random noise.
    for label, sent, digest_of in (
        ("one byte flipped", b"rsid\tbeta\nrs1\t0.2\n", payload),
        ("truncated", payload[:-4], payload),
        ("a different file entirely", b"unrelated\n", payload),
    ):
        expect_request_error(
            f"inputs: sha256 does not match the decoded bytes ({label}) -> 400 DigestMismatch",
            _parse(inputs=[_element("covid.tsv", sent,
                                    digest=hashlib.sha256(digest_of).hexdigest())]),
            400, "DigestMismatch")

    for label, value in (("a charset parameter", "text/csv; charset=utf-8"),
                         ("no space before the parameter", "text/csv;charset=utf-8"),
                         ("a quoted parameter value", 'application/x-tar; name="a b"')):
        accepted = sup.parse_execute_request(json.dumps(make_body(
            inputs=[dict(good, content_type=value)])).encode()).inputs[0].content_type
        check(f"inputs: content_type with {label} is accepted verbatim",
              accepted == value, f"got {accepted!r}")

    for label, value in (("no slash", "text"), ("an empty subtype", "text/"),
                         ("an empty type", "/csv"), ("a non-string", 7),
                         ("a parameter with no value", "text/csv; charset"),
                         ("a trailing semicolon", "text/csv; ")):
        expect_request_error(f"inputs: content_type with {label} -> 400",
                             _parse(inputs=[dict(good, content_type=value)]),
                             400, "InvalidRequest")

    # -- the caps, each as its own status. 413, never 400: a cap is not a malformed request.
    # InputsTooLarge, never PayloadTooLarge: the body cap answers with that one, and a caller
    # that cannot tell them apart cannot tell "drop an input" from "send a smaller request".
    expect_request_error(
        f"inputs: {sup.MAX_INPUTS + 1} elements -> 413 InputsTooLarge",
        _parse(inputs=[_element(f"f{i}.txt", b"x") for i in range(sup.MAX_INPUTS + 1)]),
        413, sup.ERR_INPUTS_TOO_LARGE)
    check(f"inputs: {sup.MAX_INPUTS} elements is the boundary and is accepted",
          len(sup.parse_execute_request(json.dumps(make_body(
              inputs=[_element(f"f{i}.txt", b"x") for i in range(sup.MAX_INPUTS)])).encode()
          ).inputs) == sup.MAX_INPUTS)

    at_cap = b"z" * sup.MAX_INPUT_BYTES
    check("inputs: exactly MAX_INPUT_BYTES is accepted",
          sup.parse_execute_request(json.dumps(make_body(
              inputs=[_element("big.bin", at_cap)])).encode()).inputs[0].data == at_cap)
    expect_request_error(
        "inputs: one byte over MAX_INPUT_BYTES -> 413 InputsTooLarge",
        _parse(inputs=[_element("big.bin", b"z" * (sup.MAX_INPUT_BYTES + 1))]),
        413, sup.ERR_INPUTS_TOO_LARGE)

    half = sup.MAX_INPUTS_TOTAL_BYTES // 2
    check("inputs: exactly MAX_INPUTS_TOTAL_BYTES across two elements is accepted",
          len(sup.parse_execute_request(json.dumps(make_body(inputs=[
              _element("a.bin", b"a" * half), _element("b.bin", b"b" * half)])).encode()
          ).inputs) == 2)
    expect_request_error(
        "inputs: one byte over MAX_INPUTS_TOTAL_BYTES, with every element under its own "
        "cap -> 413 InputsTooLarge",
        _parse(inputs=[_element("a.bin", b"a" * half),
                       _element("b.bin", b"b" * (half + 1))]),
        413, sup.ERR_INPUTS_TOO_LARGE)

    # The one-sided deploy. An unknown TOP-LEVEL field still fails on the first call, which is
    # what makes a client that ships before the supervisor visible rather than silently ignored.
    expect_request_error("inputs: an unknown top-level field alongside inputs -> 400 "
                         "UnknownField, so a one-sided deploy fails on the first call",
                         _parse(inputs=[good], input_files=[good]), 400, "UnknownField")
    check("inputs: `inputs` is in the accepted top-level field set",
          "inputs" in sup._EXECUTE_FIELDS, f"got {sorted(sup._EXECUTE_FIELDS)}")


def _raw_post(host, port, payload):
    """POST a body the supervisor may refuse on Content-Length alone.

    http.client cannot carry this case: the oversize branch never reads the body and closes the
    connection, so the send races the close. Tolerating a reset mid-send is the point — what is
    under test is the ANSWER, and a caller that gets a reset instead of a 413 learns nothing.
    """
    s = socket.create_connection((host, port), timeout=30)
    head = (f"POST /execute HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n")
    raw = b""
    try:
        s.sendall(head.encode())
        try:
            s.sendall(payload)
        except OSError:
            pass  # refused on the header alone and closed; the response is already on the way
        while True:
            block = s.recv(65536)
            if not block:
                break
            raw += block
    except (socket.timeout, OSError):
        pass
    finally:
        s.close()
    head_bytes, _, body = raw.partition(b"\r\n\r\n")
    status = int(head_bytes.split(b" ")[1]) if head_bytes else 0
    try:
        parsed = json.loads(body.decode())
    except Exception:
        parsed = None
    return status, parsed


def test_inputs_caps_on_the_wire(server):
    """Both refusals over HTTP, and that a caller can tell them apart.

    They are only just distinguishable, and the arithmetic is why: base64 inflates 4/3, so the
    inputs-total cap at 512 KiB rides in a ~700 KiB body and is reachable, while two elements
    that are each legal on their own can put the body over 1 MiB and never reach _parse_inputs
    at all. The sizes below are computed from the module's constants, not chosen, so a cap
    change moves the probes with it.
    """
    third = sup.MAX_INPUTS_TOTAL_BYTES // 3 + 1024
    over_total = make_body(inputs=[_element(f"f{i}.bin", bytes(third)) for i in range(3)])
    encoded = json.dumps(over_total).encode()
    check("inputs caps: the over-total body is still under the body cap, so the inputs cap is "
          "the one that can fire",
          len(encoded) < sup.MAX_BODY_BYTES, f"body {len(encoded)} bytes")
    status, body = _raw_post(server.host, server.port, encoded)
    total_message = ((body or {}).get("error") or {}).get("message", "")
    total_type = ((body or {}).get("error") or {}).get("type")
    check("inputs caps: 3 elements over MAX_INPUTS_TOTAL_BYTES -> 413 InputsTooLarge",
          status == 413 and total_type == sup.ERR_INPUTS_TOO_LARGE, f"got {status} {body}")
    check("inputs caps: the total refusal names the inputs cap",
          "inputs" in total_message and "total" in total_message, f"got {total_message!r}")

    big = sup.MAX_INPUT_BYTES - 112 * 1024
    over_body = make_body(inputs=[_element(f"g{i}.bin", bytes(big)) for i in range(2)])
    encoded = json.dumps(over_body).encode()
    check("inputs caps: two elements each under MAX_INPUT_BYTES can still exceed the 1 MiB "
          "body cap — the interaction a caller has to be able to see",
          len(encoded) > sup.MAX_BODY_BYTES and big <= sup.MAX_INPUT_BYTES,
          f"body {len(encoded)} bytes from 2 x {big}")
    status, body = _raw_post(server.host, server.port, encoded)
    body_message = ((body or {}).get("error") or {}).get("message", "")
    body_type = ((body or {}).get("error") or {}).get("type")
    check("inputs caps: the oversize body is 413 PayloadTooLarge, the body cap's own type",
          status == 413 and body_type == "PayloadTooLarge", f"got {status} {body}")
    # Both refusals are 413 and both carry execution_id null, so error.type is the only field a
    # caller can branch on — and the client (SandboxClient) has to tell "drop an input and
    # retry" from "your request is too big" without matching on English.
    check("inputs caps: the two refusals are distinguishable by error.type alone, not by "
          "string-matching the message",
          total_type != body_type and total_type == sup.ERR_INPUTS_TOO_LARGE
          and body_type == "PayloadTooLarge", f"total={total_type!r} body={body_type!r}")
    check("inputs caps: and each message still names its own cap",
          "body" in body_message and "inputs" in total_message
          and body_message != total_message,
          f"body={body_message!r} total={total_message!r}")
    check("inputs caps: both carry execution_id null, which is why the type has to carry the "
          "difference",
          (body or {}).get("execution_id") is None, f"got {(body or {}).get('execution_id')!r}")

    status, _, body = server.request("POST", "/execute", make_body(
        inputs=[_element(f"h{i}.txt", b"x") for i in range(sup.MAX_INPUTS + 1)]))
    check("inputs caps: too many elements is 413 InputsTooLarge over the wire, not 400",
          status == 413 and body["error"]["type"] == sup.ERR_INPUTS_TOO_LARGE,
          f"got {status} {body}")


def test_inputs_delivery(server):
    """What the child sees, what the response says it sent, and what is left afterwards."""
    tsv = b"rsid\tbeta\tse\nrs1\t0.10\t0.02\nrs2\t-0.35\t0.04\n"
    binary = bytes(range(256)) * 7          # every byte value, including NUL and 0x0a
    elements = [_element("covid.tsv", tsv, content_type="text/tab-separated-values"),
                _element("weights.bin", binary)]

    request = make_body(code=_probe(), inputs=elements)
    eid = request["execution_id"]
    status, _, body = server.request("POST", "/execute", request)
    report = _report(body)
    check("inputs delivery: the execution succeeds",
          status == 200 and body.get("status") == "ok",
          f"got {status} {body.get('status')} {body.get('error')}")
    if report is None:
        check("inputs delivery: the probe reported", False, f"output {body.get('output')!r}")
        return

    check("inputs delivery: SANDBOX_INPUTS_DIR is set in the child environment",
          report["env_set"], f"got {report['dir']!r}")
    check("inputs delivery: it names THIS execution's inputs/ under the scratch root",
          report["dir"] == os.path.join(server.scratch_root, eid, "inputs"),
          f"got {report['dir']!r}")
    check("inputs delivery: exactly the names that were sent, and nothing else",
          report["names"] == ["covid.tsv", "weights.bin"], f"got {report['names']}")
    for name, sent in (("covid.tsv", tsv), ("weights.bin", binary)):
        seen = report["files"].get(name, {})
        check(f"inputs delivery: {name} is byte-identical to what was sent",
              seen.get("size") == len(sent)
              and seen.get("sha256") == hashlib.sha256(sent).hexdigest(),
              f"got {seen}")
        check(f"inputs delivery: {name} is written 0400", seen.get("mode") == 0o400,
              f"got {oct(seen.get('mode', 0))}")
    check("inputs delivery: the directory is an ordinary 0700, like every other per-execution "
          "directory, so every rmtree route can empty it",
          report["dir_mode"] == 0o700, f"got {oct(report['dir_mode'])}")

    # The accident guard, as an accident guard. Same uid on both sides, so the property is not
    # "the child cannot" — it is "the child does not do it by accident".
    check("inputs delivery: overwriting a delivered file is refused, so a script saving its "
          "output over its input gets an error instead of losing the bytes it is analysing",
          report["overwrite_refused"] == "PermissionError",
          f"got {report['overwrite_refused']!r}")
    check("inputs delivery: and it succeeds after the child chmods the file, which is the "
          "honest statement of the guard — same uid, so this is not a boundary",
          report["overwrite_after_chmod"] is True, f"got {report['overwrite_after_chmod']!r}")
    check("inputs delivery: writing a NEW name into inputs/ is NOT refused — the directory is "
          "not sealed, and a seal would only have stopped unlink-then-recreate anyway",
          report["create_refused"] is False, f"got {report['create_refused']!r}")

    echo = body.get("inputs")
    check("inputs echo: one row per delivered input, in the order sent",
          echo == [{"name": "covid.tsv", "size": len(tsv)},
                   {"name": "weights.bin", "size": len(binary)}], f"got {echo}")
    check("inputs echo: name and size only — no path, no digest, no content type",
          all(set(row) == {"name", "size"} for row in echo or []), f"got {echo}")
    check("inputs echo: the sizes are the DECODED lengths, which is what makes a truncated "
          "delivery visible to the caller",
          [row["size"] for row in echo or []] == [len(tsv), len(binary)], f"got {echo}")
    check("inputs delivery: inputs are not collected as artifacts",
          body.get("artifacts") == [], f"got {body.get('artifacts')}")

    if server.host_scratch is None:
        skip("inputs lifecycle: nothing outlives the execution",
             "no host view of /scratch in container mode")
    else:
        base = os.path.join(server.host_scratch, eid)
        check("inputs lifecycle: inputs/ is gone once the response is built",
              not os.path.exists(os.path.join(base, "inputs")),
              f"{sorted(os.listdir(base)) if os.path.isdir(base) else 'no directory'}")
        check("inputs lifecycle: the retained directory holds artifacts/ and nothing else",
              sorted(os.listdir(base)) == ["artifacts"] if os.path.isdir(base) else False,
              f"{sorted(os.listdir(base)) if os.path.isdir(base) else 'no directory'}")

    # -- an execution that sent nothing still finds the directory, empty -----------------
    request = make_body(code=_probe(peer=report["dir"]))
    status, _, body = server.request("POST", "/execute", request)
    empty = _report(body)
    check("inputs: an execution with no inputs still gets the directory",
          empty is not None and empty["env_set"] and empty["names"] == [],
          f"got {empty}")
    check("inputs: with the same mode, so `no inputs` and `inputs` are one shape",
          empty is not None and empty["dir_mode"] == 0o700,
          f"got {empty and oct(empty['dir_mode'])}")
    check("inputs echo: an execution with no inputs echoes an empty list, not a missing field",
          body.get("inputs") == [], f"got {body.get('inputs')!r}")
    check("inputs isolation: the previous execution's inputs directory is not reachable from "
          "this one",
          empty is not None and empty["peer_exists"] is False,
          f"got {empty and empty['peer_exists']}")

    # -- what the response says about where a script's own writes went ------------------
    # Measured while writing this: `stray_writes` came back EMPTY for a script that really did
    # save a file to its working directory, so the wire probe alone is weak evidence about the
    # input names — an always-empty list names nothing either way. The structural check in
    # test_inputs_quota is what carries the property; this one stays because it is the check
    # that goes red if an input name ever starts appearing there.
    stray = ("open(os.path.join(os.getcwd(), 'results.csv'), 'w').write('a,b\\n')\n")
    status, _, body = server.request("POST", "/execute", make_body(
        code=_probe(extra=stray), inputs=[_element("covid.tsv", tsv)]))
    check("inputs: no delivered input is reported as a stray write",
          "covid.tsv" not in (body.get("stray_writes") or []),
          f"got {body.get('stray_writes')}")

    art = ("open(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'plot.png'), 'wb')"
           ".write(b'\\x89PNG')\n")
    status, _, body = server.request("POST", "/execute", make_body(
        code=_probe(extra=art), inputs=[_element("covid.tsv", tsv)]))
    names = [entry["name"] for entry in body.get("artifacts") or []]
    check("inputs: the manifest collects what the script saved and NOT what was delivered",
          names == ["plot.png"], f"got {names}")


def test_inputs_quota(tmp):
    """The delivered bytes are charged to the execution, with no separate accounting."""
    root = os.path.join(tmp, "inputs-quota")
    os.makedirs(root)
    dirs = sup.ExecutionDirs(root, "33333333-3333-4333-8333-333333333333")
    dirs.create()
    before = sup._dir_usage(dirs.base, sup.EXECUTION_ENTRY_BUDGET)[0]
    payload = b"q" * (128 * 1024)
    job = types.SimpleNamespace(
        dirs=dirs, req=types.SimpleNamespace(inputs=(sup.InputFile("big.bin", payload, None),)))
    sup._deliver_inputs(job)
    after, entries, (art_cost, _) = sup._dir_usage(
        dirs.base, sup.EXECUTION_ENTRY_BUDGET, sub=dirs.artifacts)
    check("inputs quota: the bytes show up in the walk of the execution directory, which is "
          "the number both the per-execution quota and the aggregate ceiling read",
          after - before >= len(payload), f"{before} -> {after}")
    check("inputs quota: and NOT in the artifacts subtree, which is measured in the same pass",
          art_cost == 0, f"got {art_cost}")
    sup._discard_inputs(dirs)

    # The two comparisons that consume that number. Structural, because nothing at runtime can
    # distinguish "inputs were charged" from "the quota never fired" on a 192 MiB budget.
    tree = ast.parse(open(SUPERVISOR_PY, encoding="utf-8").read())
    watchdog = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "_watchdog"), None)
    if watchdog is None:
        _harness_error("_watchdog() is no longer a module-level function; the quota check "
                       "cannot be built, so this run proves nothing — fix the anchor.")
    usage_args = [n.args[0] for n in ast.walk(watchdog)
                  if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_dir_usage"]
    check("inputs quota: the watchdog measures the whole execution directory, not artifacts/ "
          "alone, so inputs/ is inside what it walks",
          [ast.unparse(a) for a in usage_args] == ["dirs.base"],
          f"got {[ast.unparse(a) for a in usage_args]}")
    # The other half of "not collected, not reported": the stray-write scan reads the script's
    # working directory and nothing else, so inputs/ is outside what it can name whatever the
    # field's own reachability turns out to be.
    stray_args = [n.args[0] for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_stray_writes"]
    check("inputs quota: _stray_writes is only ever pointed at the script's working directory, "
          "so a delivered input cannot be reported as a misplaced save",
          [ast.unparse(a) for a in stray_args] == ["dirs.tmp"],
          f"got {[ast.unparse(a) for a in stray_args]}")

    names = {n.id for n in ast.walk(watchdog) if isinstance(n, ast.Name)}
    check("inputs quota: that one walk feeds both the per-execution quota and the aggregate "
          "ceiling",
          {"EXECUTION_TOTAL_QUOTA_BYTES", "SCRATCH_AGGREGATE_CEILING_BYTES"} <= names,
          f"got {sorted(names & {'EXECUTION_TOTAL_QUOTA_BYTES', 'SCRATCH_AGGREGATE_CEILING_BYTES'})}")


def test_inputs_delivery_units(tmp):
    """The write itself: a name that already exists is a fact about something else.

    /scratch is writable by every process at the shared uid, so the target name can exist
    before the write does — as a plain file or as a symlink pointing anywhere the supervisor
    can reach. O_EXCL and O_NOFOLLOW are what make both of those a refusal instead of a write
    to somebody else's file.
    """
    root = os.path.join(tmp, "inputs-units")
    os.makedirs(root)
    dirs = sup.ExecutionDirs(root, "55555555-5555-4555-8555-555555555555")
    dirs.create()
    job = types.SimpleNamespace(
        dirs=dirs,
        req=types.SimpleNamespace(inputs=(sup.InputFile("covid.tsv", b"delivered", None),)))

    squatted = os.path.join(dirs.inputs, "covid.tsv")
    with open(squatted, "wb") as handle:
        handle.write(b"planted")
    try:
        sup._deliver_inputs(job)
        raised = None
    except OSError as exc:
        raised = type(exc).__name__
    check("inputs delivery: a name that already exists is refused, not overwritten",
          raised == "FileExistsError" and open(squatted, "rb").read() == b"planted",
          f"raised {raised}")
    os.unlink(squatted)

    victim = os.path.join(root, "not-an-input")
    with open(victim, "wb") as handle:
        handle.write(b"untouched")
    os.symlink(victim, squatted)
    try:
        sup._deliver_inputs(job)
        raised = None
    except OSError as exc:
        raised = type(exc).__name__
    check("inputs delivery: a symlink planted at the name is refused and NOT followed",
          raised in ("FileExistsError", "OSError", "ELOOP")
          and open(victim, "rb").read() == b"untouched",
          f"raised {raised}, target now {open(victim, 'rb').read()!r}")
    os.unlink(squatted)
    sup._discard_inputs(dirs)


def test_inputs_crash_leftover_control(tmp):
    """The crash path: a killed execution's directory, and whether the startup wipe can take it.

    A SIGKILL or an OOM kill runs nothing on the completion path, so `inputs/` is still there
    with the delivered bytes in it. /scratch is an emptyDir and survives a container restart, so
    the only thing between those bytes and a LATER execution reading them is
    wipe_unrecognised_scratch — an rmtree, like every other route that removes an execution
    directory. The CONTROL puts back the 0500 seal inputs/ used to carry and shows the same
    leftover surviving that wipe, silently: the sweep logs and carries on.
    """
    root = os.path.join(tmp, "inputs-crash")
    os.makedirs(root)

    def _killed(execution_id, deliver):
        dirs = sup.ExecutionDirs(root, execution_id)
        dirs.create()
        deliver(types.SimpleNamespace(
            dirs=dirs,
            req=types.SimpleNamespace(inputs=(sup.InputFile("secret.tsv", b"delivered", None),))))
        return dirs

    dirs = _killed("44444444-4444-4444-8444-444444444444", sup._deliver_inputs)
    removed = sup.wipe_unrecognised_scratch(root)
    check("inputs crash path: an execution killed before any cleanup ran leaves its directory "
          "behind, and the startup wipe removes it — delivered bytes and all",
          removed == [os.path.basename(dirs.base)] and not os.path.exists(dirs.base),
          f"removed {removed}, base exists {os.path.exists(dirs.base)}")

    if os.geteuid() == 0:
        skip("inputs crash path: CONTROL — a sealed inputs/ survives the wipe",
             "running as root, which unlinks straight through a 0500 directory")
        return
    sealed = _killed("44444444-4444-4444-8444-444444444445", _cut("seal-the-directory"))
    leftover = os.path.join(sealed.inputs, "secret.tsv")
    with _LogCapture() as log:
        removed = sup.wipe_unrecognised_scratch(root)
    check("inputs crash path: CONTROL — with inputs/ sealed 0500 the wipe cannot unlink the "
          "delivered bytes, so they are readable by the next execution the pod runs",
          removed == [] and os.path.exists(leftover)
          and open(leftover, "rb").read() == b"delivered",
          f"removed {removed}, leftover exists {os.path.exists(leftover)}")
    check("inputs crash path: CONTROL — a log line is the only evidence the wipe produces; "
          "it does not fail and nothing on the wire says anything",
          any("could not wipe stale /scratch entry" in line for line in log.lines),
          f"got {[l for l in log.lines if 'scratch' in l]}")
    os.chmod(sealed.inputs, 0o700)
    shutil.rmtree(sealed.base, ignore_errors=True)
    check("inputs crash path: CONTROL — unsealed, the same tree goes",
          not os.path.exists(sealed.base), "base still present")


def test_inputs_leak_control(tmp):
    """The same hazard on the ORDINARY path: the seal restored across a real execution.

    Not a duplicate of the crash-path control above. That one shows the startup wipe cannot
    unlink out of a sealed directory; this one shows the supervisor's own completion routes —
    _retain, and _forget_retained behind it — are the rmtrees that then fail, so the delivered
    bytes stay on /scratch at a shared uid until the pod dies.
    """
    root = os.path.join(tmp, "inputs-leak")
    os.makedirs(root)
    if os.geteuid() == 0:
        skip("inputs lifecycle: CONTROL — with inputs/ sealed the bytes outlive the "
             "execution", "running as root, which unlinks straight through a 0500 directory")
        return
    server = Server(root)
    secret = b"delivered-bytes-that-must-not-outlive-the-execution"
    try:
        real_deliver = sup._deliver_inputs
        sup._deliver_inputs = _cut("seal-the-directory")
        try:
            request = make_body(code="print('ran')", inputs=[_element("secret.tsv", secret)])
            eid = request["execution_id"]
            with _LogCapture() as log:
                status, _, body = server.request("POST", "/execute", request)
        finally:
            sup._deliver_inputs = real_deliver
        leftover = os.path.join(root, eid, "inputs", "secret.tsv")
        check("inputs lifecycle: CONTROL — the execution still answers ok, so nothing on the "
              "wire says the cleanup failed",
              status == 200 and body.get("status") == "ok", f"got {status} {body.get('status')}")
        check("inputs lifecycle: CONTROL — with inputs/ sealed, neither _discard_inputs nor "
              "_retain can unlink the delivered bytes, so they are still on /scratch",
              os.path.exists(leftover) and open(leftover, "rb").read() == secret,
              f"exists={os.path.exists(leftover)}")
        check("inputs lifecycle: CONTROL — _retain says so in the log, which is the only "
              "evidence the failure produces",
              any("could not delete inputs" in line for line in log.lines),
              f"got {[l for l in log.lines if 'inputs' in l]}")
        server.supervisor._forget_retained(eid)
        check("inputs lifecycle: CONTROL — _forget_retained cannot remove it either, so the "
              "leftover survives the TTL, both ceilings and the orphan sweep",
              os.path.exists(leftover), "the directory went after all")
        os.chmod(os.path.join(root, eid, "inputs"), 0o700)
        shutil.rmtree(os.path.join(root, eid), ignore_errors=True)

        # The same probe against the unedited path. Without this the control above proves only
        # that a mutant leaks.
        request = make_body(code="print('ran')", inputs=[_element("secret.tsv", secret)])
        eid = request["execution_id"]
        status, _, body = server.request("POST", "/execute", request)
        base = os.path.join(root, eid)
        check("inputs lifecycle: with the real _deliver_inputs the same execution leaves no "
              "inputs/ behind",
              status == 200 and not os.path.exists(os.path.join(base, "inputs")),
              f"{sorted(os.listdir(base)) if os.path.isdir(base) else 'no directory'}")
        server.supervisor._forget_retained(eid)
        check("inputs lifecycle: and the whole execution directory goes on the next sweep",
              not os.path.exists(base), "base still present")
    finally:
        server.close()


def test_inputs_digest_control(tmp):
    """CONTROL: with the digest comparison cut, corrupted bytes reach the child.

    The digest is the whole of the integrity story for this channel — the fetch happens in
    another pod, the supervisor never sees the source, and nothing downstream re-reads the file
    — so a check that only proves "a mismatch is refused" leaves open whether the refusal is
    what stops the bytes.
    """
    root = os.path.join(tmp, "inputs-digest")
    os.makedirs(root)
    server = Server(root)
    try:
        honest = b"rsid\tbeta\nrs1\t0.10\n"
        corrupted = b"rsid\tbeta\nrs1\t0.99\n"
        element = _element("covid.tsv", corrupted,
                           digest=hashlib.sha256(honest).hexdigest())
        code = ("import hashlib, os\n"
                "d = os.environ['SANDBOX_INPUTS_DIR']\n"
                "blob = open(os.path.join(d, 'covid.tsv'), 'rb').read()\n"
                "print('SHA', hashlib.sha256(blob).hexdigest())\n")

        request = make_body(code=code, inputs=[element])
        eid = request["execution_id"]
        status, _, body = server.request("POST", "/execute", request)
        check("inputs digest: a corrupted fetch is refused with 400 DigestMismatch",
              status == 400 and body["error"]["type"] == "DigestMismatch", f"got {status} {body}")
        check("inputs digest: and no execution directory is created for it, so the refusal is "
              "before any byte is written",
              not os.path.exists(os.path.join(root, eid)), "a directory was created")

        real_parse = sup._parse_inputs
        sup._parse_inputs = _cut("no-digest-check")
        try:
            status, _, body = server.request(
                "POST", "/execute", make_body(code=code, inputs=[element]))
        finally:
            sup._parse_inputs = real_parse
        check("inputs digest: CONTROL — with the comparison cut, the corrupted bytes are "
              "delivered and the analysis runs on them",
              status == 200 and f"SHA {hashlib.sha256(corrupted).hexdigest()}"
              in (body.get("output") or ""),
              f"got {status} {(body.get('output') or '')[:120]!r}")
    finally:
        server.close()
