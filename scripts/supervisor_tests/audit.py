import ast
import os
import re
import threading
import uuid

from .harness import ROOT, _StdoutCapture, check, make_body, skip, sup


PROBE = r"""
import json, os, sys
out = {"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd(),
       "env": {k: os.environ.get(k) for k in
               ("GENETICS_PREWARM", "GENETICS_MPLCACHE", "GENETICS_SCHEMA_DIR",
                "GENETICS_STUBS_DIR", "SANDBOX_SCRATCH_ROOT", "GENETICS_API_URL",
                "BIGQUERY_API_URL", "INTERNAL_API_SECRET", "SANDBOX_TOKEN_SIGNING_KEY",
                "TMPDIR")}}
def writable(path):
    try:
        with open(path, "w") as fh:
            fh.write("x")
        os.unlink(path)
        return None
    except OSError as exc:
        return exc.errno
out["write_rootfs"] = writable("/genetics/probe")
out["write_tmp"] = writable("/tmp/probe")
out["write_tmpdir"] = writable(os.path.join(os.environ["TMPDIR"], "probe"))
for mod in ("pip", "setuptools", "google.auth", "genetics_mcp_server.sdk", "matplotlib"):
    try:
        __import__(mod)
        out["import_" + mod.replace(".", "_")] = True
    except Exception:
        out["import_" + mod.replace(".", "_")] = False
out["sdk_pkg_dirs"] = sorted(os.listdir(os.path.dirname(os.path.dirname(
    sys.modules["genetics_mcp_server.sdk"].__file__)))) if "genetics_mcp_server.sdk" in sys.modules else []
print("PROBE " + json.dumps(out))
"""


# The analyzer's own regex, COPIED and not imported (genetics-mcp-server's
# scripts/analyze_conversations.py, SDK_CALL_RE). The two repos cannot share a module — the
# sandbox image installs only the SDK's import closure — so a divergence between what the
# supervisor emits and what the shipped parser reads has no other way to surface. A copy allowed
# to drift silently would be worse than none, which is what check_analyzer_regex_copy is for.
ANALYZER_SDK_CALL_RE = re.compile(
    r"\[user=(?P<user>[^\]]*)\] \[session=(?P<session>[^\]]*)\] \[execution=(?P<execution>[^\]]*)\] "
    r"Executing SDK function: (?P<function>\S+) with input: (?P<arguments>.*?) "
    r"rows: (?P<rows>\d+)(?: error: (?P<error>\S+))?(?P<cancelled> cancelled)?$"
)


ANALYZER_REL_PATH = os.path.join(
    "src", "genetics_mcp_server", "scripts", "analyze_conversations.py")


def _sibling_analyzer_path():
    """genetics-mcp-server's analyze_conversations.py, if a checkout of it sits beside this
    one — plain sibling layout, and the `.claude/worktrees/<branch>` layout both repos use,
    where the sibling's matching worktree is preferred over its main checkout."""
    repo = ROOT
    parts = repo.split(os.sep)
    candidates = []
    if len(parts) >= 3 and parts[-3:-1] == [".claude", "worktrees"]:
        main = os.sep.join(parts[:-3])
        sibling = os.path.join(os.path.dirname(main), "genetics-mcp-server")
        candidates.append(os.path.join(sibling, ".claude", "worktrees", parts[-1],
                                       ANALYZER_REL_PATH))
        candidates.append(os.path.join(sibling, ANALYZER_REL_PATH))
    candidates.append(os.path.join(os.path.dirname(repo), "genetics-mcp-server",
                                   ANALYZER_REL_PATH))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _analyzer_sdk_call_pattern(path):
    """SDK_CALL_RE's pattern as the analyzer spells it, read from source rather than imported:
    importing it would pull in the whole analyzer's dependencies, which this harness has no
    business needing."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "SDK_CALL_RE" for t in node.targets):
            continue
        if isinstance(node.value, ast.Call) and node.value.args:
            try:
                return ast.literal_eval(node.value.args[0])
            except ValueError:
                return None
    return None


def check_analyzer_regex_copy():
    """The copy above is only useful if it is still the analyzer's regex.

    Watching the supervisor side alone is half a check: if the ANALYZER moves, every assertion
    built on the copy keeps passing against a pattern nothing ships. So this reads the literal
    off disk when a sibling checkout is there, and says by name when there is not."""
    name = "audit: the copied analyzer regex still matches the shipped analyzer's own"
    path = _sibling_analyzer_path()
    if path is None:
        skip(name, "no genetics-mcp-server checkout beside this one to read SDK_CALL_RE from")
        return
    pattern = _analyzer_sdk_call_pattern(path)
    check(name, pattern == ANALYZER_SDK_CALL_RE.pattern,
          f"{path}: {pattern!r} != {ANALYZER_SDK_CALL_RE.pattern!r}")


SDK_LINE = (
    "2026-01-01 00:00:00,000 - genetics_mcp_server.sdk.audit - INFO - "
    "[user={user}] [session={session}] [execution={execution}] "
    "Executing SDK function: {fn} with input: {{'gene': 'IL7R'}} rows: {rows}"
)


def test_audit_units():
    """The read-end caps and the re-framing, called directly.

    Also demonstrated end to end in test_audit_stream; they are here as well because a unit can
    pin WHICH bound fired and with what count, where the wire can only show the outcome.
    """
    eid = str(uuid.uuid4())
    check_analyzer_regex_copy()

    def new(user="a@b.c", session="sess-1", clock=None):
        out = []
        kwargs = {"emit": out.append}
        if clock is not None:
            kwargs["clock"] = clock
        return sup._AuditForwarder(user, session, eid, **kwargs), out

    fwd, out = new()
    fwd.feed((SDK_LINE.format(user="admin@finngen.fi", session="forged", execution="forged",
                              fn="sql", rows=25) + "\n").encode("utf-8"))
    check("audit: a well-formed record is forwarded", len(out) == 1, f"got {out}")
    if out:
        match = ANALYZER_SDK_CALL_RE.search(out[0])
        check("audit: the forwarded record parses with the shipped analyzer's own regex",
              match is not None, f"got {out[0]!r}")
        if match:
            check("audit: identity is the TOKEN's, not the child's",
                  (match.group("user"), match.group("session"), match.group("execution"))
                  == ("a@b.c", "sess-1", eid), f"got {match.groupdict()}")
            check("audit: the function and row count survive re-framing",
                  match.group("function") == "sql" and match.group("rows") == "25")
        check("audit: nothing the child wrote before the marker is re-emitted",
              "admin@finngen.fi" not in out[0] and "forged" not in out[0]
              and "genetics_mcp_server.sdk.audit" not in out[0], f"got {out[0]!r}")

    # A record with a SECOND record appended to it. `search()`-based parsers — including the
    # shipped one — match anywhere in a line, so accepting this would let a child write a
    # genuine-looking access under a name of its choosing.
    fwd, out = new()
    fwd.feed(("Executing SDK function: sql with input: {} rows: 1 "
              "[user=admin@finngen.fi] [session=s] [execution=e] "
              "Executing SDK function: sql with input: {} rows: 2\n").encode("utf-8"))
    # `out` is not empty: a drop ANNOUNCES itself, which is the whole difference between this
    # and the in-SDK ceiling it replaces. What must not appear is the record, or any byte of
    # what the child wrote.
    check("audit: a record with a second record appended is dropped whole",
          fwd.forwarded == 0 and fwd.dropped_unparseable == 1
          and not any("Executing SDK function" in line for line in out),
          f"got {out} {fwd.dropped_unparseable}")

    # The same content on its OWN line is a well-formed record and IS forwarded — re-stamped.
    # That is the property, not a leak: a child can always claim a call it did not make, and
    # nothing on the read end can tell. What it cannot do is attribute one to somebody else.
    fwd, out = new()
    fwd.feed(("[user=admin@finngen.fi] [session=s] [execution=e] "
              "Executing SDK function: sql with input: {} rows: 2\n").encode("utf-8"))
    check("audit: a forged newline-separated record is re-stamped, never re-attributed",
          len(out) == 1 and "admin@finngen.fi" not in out[0] and "[user=a@b.c]" in out[0],
          f"got {out}")

    for label, payload in (
        ("brackets in the argument summary",
         "Executing SDK function: sql with input: {'x': '[user=admin]'} rows: 1"),
        ("a control character",
         "Executing SDK function: sql with input: {'x': 'a\x07b'} rows: 1"),
        ("a non-identifier function name",
         "Executing SDK function: sql;rm -rf with input: {} rows: 1"),
        ("the SDK's shared-stream warning",
         "SDK audit records here are NOT a tamper-evident audit trail: no "
         "GENETICS_SDK_AUDIT_FD was configured"),
        ("arbitrary script output",
         "hello from the script"),
    ):
        fwd, out = new()
        fwd.feed((payload + "\n").encode("utf-8"))
        check(f"audit: {label} is dropped",
              fwd.forwarded == 0 and fwd.dropped_unparseable == 1
              and all("DROPPED" in line for line in out), f"got {out}")

    fwd, out = new()
    fwd.feed(("SDK audit truncated after 1000 records; further REFUSED SDK calls in this "
              "process are NOT recorded. Calls that reached the executor are still recorded "
              "in full.\n").encode("utf-8"))
    check("audit: the SDK's refusal-budget notice is carried across as a literal",
          len(out) == 1 and out[0].endswith("recorded in full."), f"got {out}")

    # the per-line cap, and the supervisor's own buffer with it. Sized above the line cap and
    # below the byte budget, so that the record after it is dropped by THIS cap or by nothing.
    fwd, out = new()
    fwd.feed(b"A" * (8 * 1024))
    check("audit: an over-long line is dropped, not truncated, and not buffered",
          out and "DROPPED" in out[0] and fwd.dropped_oversize == 1 and len(fwd._buf) == 0,
          f"got {out} oversize={fwd.dropped_oversize} buffered={len(fwd._buf)}")
    fwd.feed(b"tail of the long line\n" + (SDK_LINE.format(
        user="u", session="s", execution="e", fn="coloc", rows=3) + "\n").encode("utf-8"))
    check("audit: a genuine record after an over-long one is still forwarded",
          any("Executing SDK function: coloc" in line for line in out), f"got {out}")

    # the rate cap, on a frozen clock so the bucket cannot refill
    fwd, out = new(clock=lambda: 1000.0)
    record = (SDK_LINE.format(user="u", session="s", execution="e", fn="sql", rows=1)
              + "\n").encode("utf-8")
    fwd.feed(record * (sup.AUDIT_RATE_BURST + 50))
    forwarded = [line for line in out if "Executing SDK function:" in line]
    check("audit: the rate cap drops past the burst and announces itself once",
          len(forwarded) == sup.AUDIT_RATE_BURST and fwd.dropped_rate == 50
          and sum("records/s" in line for line in out) == 1,
          f"got forwarded={len(forwarded)} dropped={fwd.dropped_rate}")

    # the byte budget, on a clock that advances so the rate cap cannot be what fires
    ticks = iter(range(10 ** 6))
    fwd, out = new(clock=lambda: float(next(ticks)))
    while fwd.bytes_seen <= sup.AUDIT_STREAM_MAX_BYTES + 4096:
        fwd.feed(record * 64)
    check("audit: the per-execution byte budget drops past its cap and announces itself",
          fwd.dropped_over_budget > 0 and sum("byte per-execution budget" in line
                                              for line in out) == 1,
          f"got over_budget={fwd.dropped_over_budget}")

    # The budget must not truncate a record and forward the fragment. The child owns every byte
    # on the fd, so it owns where the boundary falls, and a forwarded prefix parses as a
    # DIFFERENT record than the child wrote (`rows: 999999999` -> `rows: 9`) under the real
    # user's stamp, counted as forwarded. The pad is oversize lines deliberately: they are
    # dropped whole and spend no rate token.
    ticks = iter(range(10 ** 6))
    fwd, out = new(clock=lambda: float(next(ticks)))
    trap = b"Executing SDK function: sql with input: {} rows: 999999999\n"
    room = trap.index(b"rows: ") + len("rows: ") + 1     # the cut keeps one digit: `rows: 9`
    fwd.feed(b"x" * (sup.AUDIT_STREAM_MAX_BYTES - room - 1) + b"\n")
    fwd.feed(trap)
    fwd.close()
    check("audit: the byte budget drops a cut record rather than forwarding the fragment",
          fwd.forwarded == 0 and not any("Executing SDK function" in line for line in out)
          and fwd.dropped_over_budget == 1,
          f"got forwarded={fwd.forwarded} over_budget={fwd.dropped_over_budget} {out}")

    # A flood past the budget carrying NO newline at all: counting newlines alone reported
    # dropped_over_budget=0 and left `bytes=` as the only evidence anything was lost.
    ticks = iter(range(10 ** 6))
    fwd, out = new(clock=lambda: float(next(ticks)))
    while fwd.bytes_seen <= sup.AUDIT_STREAM_MAX_BYTES + 65536:
        fwd.feed(b"z" * 65536)
    fwd.close()
    check("audit: a newline-free flood past the budget is counted, not only weighed",
          fwd.dropped_over_budget >= 1, f"got over_budget={fwd.dropped_over_budget}")

    # `<unavailable>` — the bare string, no braces — is what _summarize_arguments returns when
    # signature.bind_partial raises, i.e. whenever a script passes one extra positional or one
    # unknown keyword. An ordinary buggy script: dropping it put a genuine record into
    # dropped_unparseable, where an operator reads it as tampering.
    for label, payload in (
        ("an executed call", "Executing SDK function: gene with input: <unavailable> "
                             "rows: 0 error: TypeError"),
        ("a rejected call", "Rejected SDK function: gene with input: <unavailable> "
                            "error: TypeError"),
    ):
        fwd, out = new()
        fwd.feed((payload + "\n").encode("utf-8"))
        check(f"audit: the SDK's <unavailable> argument summary is a record on {label}",
              fwd.forwarded == 1 and fwd.dropped_unparseable == 0
              and out and out[0].endswith(payload), f"got {out}")

    # The argument charset is the SDK's, which is ASCII, and the read end is where that has to
    # be held: `<type>` renders type(value).__name__, which a script owns. U+2028/U+2029/U+0085
    # each split the record into two lines under str.splitlines().
    for label, payload in (
        ("a U+2028 line separator", "\u2028"),
        ("a U+2029 paragraph separator", "\u2029"),
        ("a U+0085 next-line", "\u0085"),
        ("a U+202E right-to-left override", "\u202e"),
        ("a non-breaking space", "\u00a0"),
        ("fullwidth brackets", "\uff3b\uff3d"),
    ):
        fwd, out = new()
        fwd.feed(("Executing SDK function: sql with input: {'x': 'a%sb'} rows: 1\n" % payload)
                 .encode("utf-8"))
        check(f"audit: {label} in the argument summary is dropped",
              fwd.forwarded == 0 and fwd.dropped_unparseable == 1
              and payload not in "".join(out), f"got {out!r}")

    # `\d` is Unicode in Python, so `rows: ١٢٣` was forwarded and the analyzer's int() read
    # back a row count nobody wrote.
    fwd, out = new()
    fwd.feed("Executing SDK function: sql with input: {} rows: \u0661\u0662\u0663\n"
             .encode("utf-8"))
    check("audit: a non-ASCII digit row count is dropped",
          fwd.forwarded == 0 and fwd.dropped_unparseable == 1, f"got {out!r}")

    # The bucket bounds what reaches an operator, so junk must not empty it: 200 pad lines
    # spent 63 tokens and left records=0.
    fwd, out = new(clock=lambda: 1000.0)
    fwd.feed(b"not a record at all\n" * 200)
    fwd.feed((SDK_LINE.format(user="u", session="s", execution="e", fn="sql", rows=4)
              + "\n").encode("utf-8"))
    check("audit: unparseable junk spends no rate tokens",
          fwd.dropped_rate == 0 and fwd.forwarded == 1 and fwd.dropped_unparseable == 200,
          f"got rate={fwd.dropped_rate} forwarded={fwd.forwarded}")

    # One stdout stream, one timestamp shape: the forwarder writes directly rather than
    # through LOG, so it has to render main()'s basicConfig %(asctime)s itself.
    with _StdoutCapture() as cap:
        sup._audit_emit("[user=a@b.c] [session=s] [execution=e] hello")
    check("audit: a forwarded record carries the same timestamp shape as a log line",
          re.match(r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO \[supervisor\.audit\] ",
                   cap.text()) is not None, f"got {cap.text()!r}")

    # identity that would break the framing — the same replace-don't-truncate rule
    # _sanitise_error_type applies to a child-supplied error.type
    fwd, out = new(user="alice\n[user=admin@finngen.fi")
    fwd.close()
    check("audit: an identity that would break the framing renders <invalid>",
          len(out) == 1 and out[0].startswith("[user=<invalid>]"), f"got {out}")

    # _drain's sink-failure path must keep reading. Dropping the stream into the buffering
    # branch raised on the next block — the audit pipe is drained with limit=None on purpose —
    # and killed the drain thread, after which nothing read the fd, the pipe filled, and a
    # running child blocked in os.write inside a call that was succeeding. The writes below
    # total more than one pipeful, so the block after the failure is really exercised.
    read_fd, write_fd = os.pipe()
    seen = []

    def angry(block):
        seen.append(block)
        raise RuntimeError("the sink is broken")

    def writer():
        try:
            os.write(write_fd, b"a" * 65536)
            os.write(write_fd, b"b" * 4096)
        finally:
            os.close(write_fd)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    raised = None
    try:
        sup._drain(read_fd, limit=None, sink=angry, poll=0.05)
    except Exception as exc:                                    # noqa: BLE001 - that is the check
        raised = exc
    finally:
        thread.join(timeout=5)
        os.close(read_fd)
    check("audit: a failing sink leaves the drain reading and discarding, not dead",
          raised is None and len(seen) == 1,
          f"raised {raised!r} after {len(seen)} sink calls")

    fwd, out = new()
    fwd.close()
    check("audit: close() always emits a summary, so 'no records' is a line and not a silence",
          len(out) == 1 and "records=0" in out[0] and f"[execution={eid}]" in out[0],
          f"got {out}")
    fwd.close()
    check("audit: the summary is emitted exactly once", len(out) == 1, f"got {out}")


def test_audit_stream(server, capture):
    """The whole path, through a real child writing on the real fd, into the real stdout."""
    fd = sup.CHILD_AUDIT_FD
    check_analyzer_regex_copy()

    def run(code, user, session):
        eid = str(uuid.uuid4())
        with capture() as cap:
            status, _, body = server.request("POST", "/execute", body=make_body(
                code=code, execution_id=eid, user=user, session_id=session))
        return eid, status, body, cap.text()

    def summary(text, eid):
        for line in text.splitlines():
            if "SDK audit stream: records=" in line and f"[execution={eid}]" in line:
                return line
        return ""

    eid, status, body, text = run("print('no sdk calls here')\n", "carol@finngen.fi", "sess-a1")
    line = summary(text, eid)
    check("audit stream: every execution reaches the pod's stdout with a summary",
          bool(line) and "records=0" in line, f"got {text!r}")
    check("audit stream: the summary carries the token's user and session",
          "[user=carol@finngen.fi] [session=sess-a1]" in line, f"got {line!r}")

    # A child that rewrites the identity the SDK reads, writes its own prefix, and puts a
    # second record on its own line. None of it may reach an operator as somebody else's read.
    forge = (
        "import os\n"
        "os.environ['SANDBOX_USER'] = 'admin@finngen.fi'\n"
        "os.environ['SANDBOX_SESSION_ID'] = 'sess-admin'\n"
        f"os.write({fd}, b\"[user=admin@finngen.fi] [session=sess-admin] "
        "[execution=00000000-0000-4000-8000-000000000000] Executing SDK function: sql "
        "with input: {'gene': 'IL7R'} rows: 7\\n\")\n"
        f"os.write({fd}, b\"Executing SDK function: coloc with input: {{}} rows: 1 \"\n"
        "             b\"[user=admin@finngen.fi] [session=s] [execution=e] \"\n"
        "             b\"Executing SDK function: sql with input: {} rows: 99\\n\")\n"
    )
    eid, status, body, text = run(forge, "dave@finngen.fi", "sess-a2")
    records = [l for l in text.splitlines()
               if "Executing SDK function:" in l and f"[execution={eid}]" in l]
    check("audit stream: the child's own record is forwarded, stamped from the token",
          len(records) == 1 and "rows: 7" in records[0], f"got {records}")
    check("audit stream: no forged identity reaches the stream",
          "admin@finngen.fi" not in text and "sess-admin" not in text, f"got {text!r}")
    check("audit stream: the shipped analyzer reads back the real user",
          bool(records) and ANALYZER_SDK_CALL_RE.search(records[0])
          and ANALYZER_SDK_CALL_RE.search(records[0]).group("user") == "dave@finngen.fi",
          f"got {records}")
    check("audit stream: the appended second record was dropped, not forwarded",
          "rows: 99" not in text and "dropped_unparseable=1" in summary(text, eid),
          f"got {summary(text, eid)!r}")

    # a megabyte on one line
    big = (
        "import os\n"
        f"os.write({fd}, b'A' * (1024 * 1024) + b'\\n')\n"
        f"os.write({fd}, b\"Executing SDK function: sumstats with input: {{}} rows: 2\\n\")\n"
    )
    eid, status, body, text = run(big, "erin@finngen.fi", "sess-a3")
    line = summary(text, eid)
    check("audit stream: a megabyte line is dropped whole and reported",
          "dropped_oversize=1" in line and "AAAAAAAA" not in text, f"got {line!r}")
    check("audit stream: the execution itself still succeeds",
          body.get("status") == "ok", f"got {status} {body}")

    # above the rate cap
    flood = (
        "import os\n"
        "rec = b\"Executing SDK function: sql with input: {'gene': 'IL7R'} rows: 1\\n\"\n"
        f"os.write({sup.CHILD_AUDIT_FD}, rec * 2000)\n"
    )
    eid, status, body, text = run(flood, "frank@finngen.fi", "sess-a4")
    line = summary(text, eid)
    dropped = int(line.split("dropped_rate=")[1].split()[0]) if "dropped_rate=" in line else 0
    forwarded = int(line.split("records=")[1].split()[0]) if "records=" in line else -1
    check("audit stream: a flood above the rate cap is bounded and counted",
          dropped > 0 and 0 < forwarded <= sup.AUDIT_RATE_BURST + sup.AUDIT_RATE_PER_S,
          f"got {line!r}")
    check("audit stream: the rate cap announces itself in the stream",
          any("records/s" in l and f"[execution={eid}]" in l for l in text.splitlines()),
          f"got {text!r}")

    # past the per-execution byte budget
    over = (
        "import os\n"
        "rec = b\"Executing SDK function: sql with input: {'gene': 'IL7R'} rows: 1\\n\"\n"
        f"os.write({sup.CHILD_AUDIT_FD}, rec * 20000)\n"
    )
    eid, status, body, text = run(over, "gina@finngen.fi", "sess-a5")
    line = summary(text, eid)
    over_budget = (int(line.split("dropped_over_budget=")[1].split()[0])
                   if "dropped_over_budget=" in line else 0)
    bytes_seen = int(line.split("bytes=")[1].split()[0]) if "bytes=" in line else 0
    check("audit stream: the per-execution byte budget fires and the reader keeps reading",
          over_budget > 0 and bytes_seen > sup.AUDIT_STREAM_MAX_BYTES, f"got {line!r}")
    check("audit stream: a child past the budget is not blocked and still exits cleanly",
          body.get("status") == "ok", f"got {status} {body}")
