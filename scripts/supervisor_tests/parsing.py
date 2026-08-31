import json
import os
import uuid

from .harness import check, expect_request_error, make_body, make_token, sup


def test_nsswitch(tmp):
    def write(text):
        path = os.path.join(tmp, f"nsswitch-{abs(hash(text))}.conf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    ok = write("passwd: files\nhosts: files [!UNAVAIL=return] dns\n")
    try:
        sup.assert_nsswitch_hosts_files_first(ok)
        check("nsswitch: files before dns passes", True)
    except sup.StartupAssertionError as exc:
        check("nsswitch: files before dns passes", False, str(exc))

    for label, text in (
        ("dns before files", "hosts: dns files\n"),
        ("no files source", "hosts: myhostname dns\n"),
        ("no hosts line", "passwd: files\n"),
    ):
        try:
            sup.assert_nsswitch_hosts_files_first(write(text))
            check(f"nsswitch: {label} rejected", False, "accepted")
        except sup.StartupAssertionError:
            check(f"nsswitch: {label} rejected", True)

    try:
        sup.assert_nsswitch_hosts_files_first(os.path.join(tmp, "absent.conf"))
        check("nsswitch: missing file rejected", False, "accepted")
    except sup.StartupAssertionError:
        check("nsswitch: missing file rejected", True)


def test_parsing():
    raw = json.dumps(make_body()).encode()
    req = sup.parse_execute_request(raw)
    check("parse: defaults timeout_s to 60", req.timeout_s == sup.DEFAULT_TIMEOUT_S,
          f"got {req.timeout_s}")

    def parse(**kw):
        return lambda: sup.parse_execute_request(json.dumps(make_body(**kw)).encode())

    expect_request_error("parse: unknown field -> 400", parse(nonsense=1), 400, "UnknownField")
    expect_request_error("parse: empty code -> 400", parse(code="   "), 400, "InvalidRequest")
    expect_request_error(
        "parse: oversized code -> 413", parse(code="#" * (sup.MAX_CODE_BYTES + 1)),
        413, "PayloadTooLarge")
    expect_request_error(
        "parse: non-uuid execution_id -> 400",
        lambda: sup.parse_execute_request(json.dumps(
            dict(make_body(), execution_id="../etc")).encode()),
        400, "InvalidRequest")
    # `^...$` with re.match accepts a trailing newline, and this field becomes a directory
    # name and is echoed in the response, so it is a log-injection primitive as well as a
    # path one.
    for label, eid in (
        ("trailing newline", "11111111-1111-4111-8111-111111111111\n"),
        ("leading newline", "\n11111111-1111-4111-8111-111111111111"),
        ("trailing CRLF", "11111111-1111-4111-8111-111111111111\r\n"),
    ):
        body = make_body(execution_id=eid)
        body["tokens"] = {a: make_token(a, eid, "a@b.c", "sess-1") for a in sup.TOKEN_AUDIENCES}
        expect_request_error(
            f"parse: execution_id with a {label} -> 400",
            lambda b=body: sup.parse_execute_request(json.dumps(b).encode()),
            400, "InvalidRequest")

    expect_request_error("parse: timeout_s 121 rejected not clamped", parse(timeout_s=121),
                         400, "InvalidRequest")
    expect_request_error("parse: timeout_s 0 rejected", parse(timeout_s=0), 400, "InvalidRequest")

    # code measured on the UTF-8 encoding of the decoded string, not the JSON escaping
    multibyte = "x = '" + ("é" * (sup.MAX_CODE_BYTES // 2 - 10)) + "'"
    check("parse: code cap measured on decoded UTF-8",
          len(multibyte.encode()) <= sup.MAX_CODE_BYTES
          and sup.parse_execute_request(json.dumps(make_body(code=multibyte)).encode()) is not None)

    eid = str(uuid.uuid4())
    body = make_body(execution_id=eid)
    body["tokens"]["db-api"] = make_token("db-api", str(uuid.uuid4()), "a@b.c", "sess-1")
    expect_request_error("parse: differing jti -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["tokens"]["db-api"] = make_token("results-api", body["execution_id"], "a@b.c", "sess-1")
    expect_request_error("parse: aud not the key it was sent under -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["user"] = "someone-else@b.c"
    expect_request_error("parse: sub != user -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["session_id"] = "other"
    expect_request_error("parse: sid != session_id -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["tokens"]["extra"] = "x"
    expect_request_error("parse: extra token key -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "InvalidRequest")

    body = make_body()
    body["tokens"]["db-api"] = "not-a-jws"
    expect_request_error("parse: malformed token -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "InvalidToken")

    # jti must equal the body's execution_id, in both directions
    eid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    body = make_body(execution_id=eid)
    body["tokens"] = {a: make_token(a, other, "a@b.c", "sess-1") for a in sup.TOKEN_AUDIENCES}
    expect_request_error("parse: jti != execution_id -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")
