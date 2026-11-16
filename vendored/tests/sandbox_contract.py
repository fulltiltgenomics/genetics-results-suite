"""The sandbox token and per-execution budget contract, as tests both validators must pass.

results-api and db-api each validate the same per-execution sandbox token and enforce the
same per-execution (`jti`) request-count and concurrency rules, in separately maintained
modules. Design of record: docs/code-execution-security.md section 4 in genetics-results-suite.
This module holds what the two test suites would otherwise copy from each other: the ASGI
driver, the token minter, the two routes every budget test drives, and the tests whose
assertions are the same for both services.

It imports neither service. A repo's test module subclasses a contract class under a `Test`
name, which is what makes pytest collect the inherited tests there, and supplies what differs
as class attributes and fixtures:

    class TestBudgetContract(BudgetContract):
        budget = sandbox_budget        # the repo's budget module
        audience = "db-api"            # the `aud` this service accepts
        ...

`BudgetContract` documents the rest of its attributes where it declares them, and needs a
`client` fixture: the ASGI app under test, with the repo's own auth
dependency and budget middleware, and `add_budget_routes` applied to it. `TokenContract` needs
an `accepts` fixture: a callable taking a bearer token and returning whether the service
authenticated it as a sandbox principal.

A test that asserts something only one service does — byte budgets, the request deadline and
the pod-wide reserve in results-api; the app-level gate in db-api — stays in that repo.

The token properties are all failure properties: a token that is expired, back-dated, missing
a claim, attributed to nobody, minted for the other service or for both at once, signed with
another key or altered after signing must be refused, and clock skew between minter and
validator must not refuse a fresh one.

The budget properties:

  * the request-count limit answers 429 on its own, and is counted per execution, not per pod;
  * a spent execution is refused before any handler work;
  * concurrency is *rejected*, not queued, per execution and pod-wide, and the request already
    in flight still completes;
  * a slot is released when its request ends, so the next one is admitted;
  * the counter map is bounded, and neither its sweep nor a full map ever evicts a live
    execution — an evicted counter is a reset budget, which is the fail-open direction;
  * a request with no credential, or with a sandbox-shaped token that does not verify, is a
    401 from the service's own auth and creates no accounting at all;
  * a limit configured to nonsense stops the process from starting.
"""

import asyncio
import base64
import json
import time
from urllib.parse import urlencode

import jwt
import pytest
from fastapi.responses import JSONResponse

SIGNING_KEY = "test-sandbox-signing-key-that-is-32-bytes+"


# --- driving an ASGI app directly ------------------------------------------------------------


def http_scope(
    method: str,
    path: str,
    *,
    query: str = "",
    headers=(),
    spec_version: str = "2.3",
) -> dict:
    """An HTTP connection scope; `headers` are (name, value) string pairs."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }


class AsgiResponse:
    """What the app sent, read back out of its raw ASGI messages."""

    def __init__(self, messages: list[dict]):
        start = next(m for m in messages if m["type"] == "http.response.start")
        self.status_code = start["status"]
        # as sent: a list of (name, value) byte pairs
        self.headers = start["headers"]
        self.content = b"".join(
            bytes(m.get("body", b"")) for m in messages if m["type"] == "http.response.body"
        )
        # for the protocol properties a status and a body cannot express
        self.messages = messages

    def json(self):
        return json.loads(self.content)


async def acall_asgi(
    app,
    method: str,
    path: str,
    *,
    query: str = "",
    headers=(),
    body: bytes = b"",
    body_chunk_size: int | None = None,
    spec_version: str = "2.3",
    disconnect_after_body: bool = False,
    tolerate=None,
) -> AsgiResponse:
    """Run one request through `app` and collect the response.

    A coroutine, because a test client that owns the event loop cannot hold one request open
    while it issues another, which every concurrency assertion needs.

    Once the request body is delivered, `receive` blocks forever, as it does for a client
    that stays connected: that is what lets StreamingResponse's disconnect listener behave
    as in production. `disconnect_after_body` reports a disconnect instead.

    `tolerate` is a predicate over an exception the app raised: where it returns true the
    response already sent is returned instead. Starlette answers a handler error with a 500
    and then re-raises it for the server to log.
    """
    size = body_chunk_size or len(body) or 1
    chunks = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    messages: list[dict] = []

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        if disconnect_after_body:
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    scope = http_scope(
        method, path, query=query, headers=headers, spec_version=spec_version
    )
    try:
        await app(scope, receive, send)
    except Exception as exc:
        if tolerate is None or not tolerate(exc):
            raise
    return AsgiResponse(messages)


def call_asgi(app, method: str, path: str, **kwargs) -> AsgiResponse:
    """`acall_asgi` for a test with no event loop of its own."""
    return asyncio.run(acall_asgi(app, method, path, **kwargs))


def bearer(token) -> list[tuple[str, str]]:
    """The headers presenting `token`; none at all when it is None."""
    return [("Authorization", f"Bearer {token}")] if token else []


def get(client, path, token=None, spec_version="2.3", **params) -> AsgiResponse:
    return call_asgi(
        client,
        "GET",
        path,
        query=urlencode(params),
        headers=bearer(token),
        spec_version=spec_version,
    )


# --- the token --------------------------------------------------------------------------------


def mint_token(
    *,
    audience,
    key=SIGNING_KEY,
    issuer="chat-backend",
    jti="exec-123",
    age=0,
    ttl=300,
    **over,
):
    """A sandbox token as chat-backend mints it; `over` replaces a claim, or drops it if None."""
    iat = int(time.time()) - age
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "user@finngen.fi",
        "sid": "session-abc",
        "jti": jti,
        "iat": iat,
        "exp": iat + ttl,
        "scope": "query:views",
    }
    for k, v in over.items():
        if v is None:
            claims.pop(k, None)
        else:
            claims[k] = v
    return jwt.encode(claims, key, algorithm="HS256")


def tamper_subject(token: str, subject: str = "attacker@example.com") -> str:
    """`token` with its `sub` rewritten and its signature left as it was."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = subject
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{forged}.{signature}"


class TokenContract:
    """What both validators must refuse, and the skew they must absorb."""

    audience = None
    other_audience = None

    def mint(self, **kwargs):
        return mint_token(**{"audience": self.audience, **kwargs})

    def test_expired_token_is_rejected(self, accepts):
        """5-minute lifetime is the whole reason a leaked token is worth little."""
        assert not accepts(self.mint(age=400, ttl=300))

    def test_backdated_iat_is_rejected(self, accepts):
        """PyJWT accepts an ancient iat as long as exp is future; a long-ttl token must not work."""
        assert not accepts(self.mint(age=3600, ttl=7200))

    @pytest.mark.parametrize("claim", ["iss", "aud", "sub", "sid", "jti", "iat", "exp", "scope"])
    def test_every_required_claim_is_required(self, accepts, claim):
        assert not accepts(self.mint(**{claim: None}))

    def test_foreign_issuer_is_rejected(self, accepts):
        assert not accepts(self.mint(issuer="someone-else"))

    def test_a_token_minted_for_the_other_service_cannot_be_replayed_here(self, accepts):
        """Same signing key, same everything, different aud. This is the replay control."""
        assert not accepts(self.mint(audience=self.other_audience))

    def test_multi_audience_token_is_rejected(self, accepts):
        """PyJWT reads a list `aud` as membership, so this would otherwise validate at BOTH
        services and one-token-per-audience would be a minter property, not a validator one."""
        assert not accepts(self.mint(audience=["db-api", "results-api"]))

    def test_single_element_list_audience_is_also_rejected(self, accepts):
        assert not accepts(self.mint(audience=[self.audience]))

    @pytest.mark.parametrize("claim", ["sub", "sid", "jti"])
    def test_empty_attribution_claim_is_rejected(self, accepts, claim):
        """`options={"require": ...}` only rejects missing/null; an empty string attributes the
        query to nobody, which defeats the point of the credential."""
        assert not accepts(self.mint(**{claim: ""}))

    def test_freshly_minted_token_from_a_slightly_fast_minter_is_accepted(self, accepts):
        """iat 3s in the future: PyJWT >= 2.10 raises ImmatureSignatureError with leeway=0, and
        the minter and the validator are different pods with different clocks."""
        assert accepts(self.mint(age=-3))

    def test_leeway_does_not_widen_the_300s_iat_bound(self, accepts):
        """The MAX_TOKEN_AGE_SECONDS check is separate from the decoder and stays exact."""
        assert accepts(self.mint(age=299, ttl=600))
        assert not accepts(self.mint(age=301, ttl=600))

    def test_wrong_signing_key_is_rejected(self, accepts):
        assert not accepts(self.mint(key="a-different-key-also-32-bytes-long!"))

    def test_tampered_payload_is_rejected(self, accepts):
        assert not accepts(tamper_subject(self.mint()))


# --- the per-execution budget ----------------------------------------------------------------


def add_budget_routes(api, handler_calls: list[str]) -> None:
    """Register the two routes every budget test drives, each recording that it ran.

    `/slow` stays inside its handler until `api.state.gate` is set and announces on
    `api.state.entered` that it got there, so a test can hold one request open.
    """
    gate = asyncio.Event()
    entered = asyncio.Event()

    @api.get("/blob")
    async def blob(n: int = 10):
        handler_calls.append("blob")
        return JSONResponse({"payload": "x" * n})

    @api.get("/slow")
    async def slow():
        handler_calls.append("slow")
        entered.set()
        await gate.wait()
        return JSONResponse({"ok": True})

    api.state.gate, api.state.entered = gate, entered


async def while_held(client, token, during):
    """Hold a request by `token` inside `/slow`, await `during()`, then let it finish.

    Returns what `during` returned and the held request's response.
    """
    held = asyncio.create_task(acall_asgi(client, "GET", "/slow", headers=bearer(token)))
    await asyncio.wait_for(client.state.entered.wait(), timeout=5)
    try:
        result = await during()
    finally:
        client.state.gate.set()
    return result, await asyncio.wait_for(held, timeout=5)


def concurrent(client, first_token, second_token):
    """Hold one request inside its handler, then issue a second and read its answer."""

    async def second():
        return await acall_asgi(
            client, "GET", "/blob", query="n=1", headers=bearer(second_token)
        )

    return asyncio.run(while_held(client, first_token, second))


class BudgetContract:
    """The per-execution rules both services enforce identically."""

    budget = None
    audience = None
    # the list the app under test was given in `add_budget_routes`
    handler_calls = None
    # a limit on `budget` and the value at which one `/blob` of `blob_size` exhausts it, and
    # what an entry shows once that request was charged. The services meter different things:
    # db-api only counts requests, results-api also counts the bytes it sent
    spent_after_one_request = None
    blob_size = None

    @staticmethod
    def still_charged(entry) -> bool:
        raise NotImplementedError

    def mint(self, **kwargs):
        return mint_token(**{"audience": self.audience, **kwargs})

    # --- the request-count limit ---

    def test_the_request_count_limit_stops_a_loop(self, client, monkeypatch):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_REQUESTS_PER_EXECUTION", 3)
        token = self.mint()

        statuses = [get(client, "/blob", token, n=1).status_code for _ in range(5)]

        assert statuses == [200, 200, 200, 429, 429]

    def test_the_request_count_429_names_its_limit(self, client, monkeypatch):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_REQUESTS_PER_EXECUTION", 1)
        token = self.mint()
        get(client, "/blob", token, n=1)
        resp = get(client, "/blob", token, n=1)

        assert resp.status_code == 429
        payload = resp.json()
        assert payload["code"] == "sandbox_request_count"
        assert (payload["limit"], payload["observed"]) == (1, 1)
        assert "Request limit for this execution" in payload["detail"]

    def test_the_count_is_per_execution_not_per_pod(self, client, monkeypatch):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_REQUESTS_PER_EXECUTION", 1)
        assert get(client, "/blob", self.mint(jti="exec-a"), n=1).status_code == 200
        assert get(client, "/blob", self.mint(jti="exec-a"), n=1).status_code == 429
        assert get(client, "/blob", self.mint(jti="exec-b"), n=1).status_code == 200

    def test_a_rejection_costs_no_handler_work(self, client, monkeypatch):
        """Admitted before the handler runs, so a spent execution costs the backend nothing."""
        monkeypatch.setattr(self.budget, *self.spent_after_one_request)
        token = self.mint()
        get(client, "/blob", token, n=self.blob_size)
        self.handler_calls.clear()

        assert get(client, "/blob", token, n=self.blob_size).status_code == 429
        assert self.handler_calls == []

    # --- concurrency: rejected, never queued ---

    def test_a_second_concurrent_request_is_rejected_not_queued(self, client, monkeypatch):
        """Queueing would burn the sandbox's wall clock on a wait the script cannot see."""
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_CONCURRENT_REQUESTS", 1)
        token = self.mint()

        second, held = concurrent(client, token, token)

        assert second.status_code == 429
        payload = second.json()
        assert payload["code"] == "sandbox_concurrency"
        assert (payload["limit"], payload["observed"]) == (1, 1)
        assert held.status_code == 200, "the held request still completes normally"
        assert self.budget.snapshot("exec-123").in_flight == 0

    def test_the_pod_wide_concurrency_limit_covers_distinct_executions(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_CONCURRENT_REQUESTS", 10)
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL", 1)

        second, held = concurrent(
            client, self.mint(jti="exec-a"), self.mint(jti="exec-b")
        )

        assert second.status_code == 429
        assert second.json()["code"] == "sandbox_concurrency_pod"
        assert held.status_code == 200

    def test_the_slot_is_released_so_the_next_request_succeeds(self, client, monkeypatch):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_CONCURRENT_REQUESTS", 1)
        token = self.mint()

        for _ in range(3):
            assert get(client, "/blob", token, n=1).status_code == 200

    # --- the counter map ---

    def test_cleanup_never_evicts_an_execution_with_a_request_in_flight(self, client):
        """A request can outlive its own token, so expiry alone is not enough to declare an
        entry dead: the sweep also requires nothing in flight."""

        async def sweep():
            with self.budget._lock:
                self.budget._sweep_locked(time.time() + 3600)
            return self.budget.snapshot("inflight")

        survived, _held = asyncio.run(
            while_held(client, self.mint(jti="inflight", ttl=1), sweep)
        )
        assert survived is not None and survived.in_flight == 1

    def test_a_full_tracker_refuses_a_new_execution_rather_than_evicting_a_live_one(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_TRACKED_EXECUTIONS", 1)
        n = self.blob_size
        assert get(client, "/blob", self.mint(jti="exec-a"), n=n).status_code == 200

        resp = get(client, "/blob", self.mint(jti="exec-b"), n=n)

        assert resp.status_code == 429
        assert resp.json()["code"] == "sandbox_execution_tracker_full"
        assert self.still_charged(self.budget.snapshot("exec-a"))

        # the slot frees itself once the incumbent's token expires, with no operator action.
        # This must use a *new* jti: re-using "exec-a" takes admit's `entry is not None` path
        # and touches neither the sweep nor the tracker-full check
        with self.budget._lock:
            self.budget._executions["exec-a"].expires_at = int(time.time()) - 3600
        assert get(client, "/blob", self.mint(jti="exec-c"), n=n).status_code == 200
        assert self.budget.snapshot("exec-a") is None, "the expired incumbent was swept"

    # --- what is never accounted ---

    def test_an_unauthenticated_request_creates_no_accounting(self, client, monkeypatch):
        monkeypatch.setattr(self.budget, "SANDBOX_MAX_REQUESTS_PER_EXECUTION", 1)
        for _ in range(3):
            assert get(client, "/blob", n=10).status_code == 401
        assert self.budget._executions == {}

    def test_an_invalid_sandbox_token_is_401_and_creates_no_accounting(self, client):
        """The gate's own resolver must not 401 or account for it; the auth dependency owns
        that."""
        forged = jwt.encode({"aud": self.audience}, "wrong-key", algorithm="HS256")
        assert get(client, "/blob", forged, n=10).status_code == 401
        assert self.budget._executions == {}

    # --- configuration ---

    @pytest.mark.parametrize("raw", ["0", "-1", "", "not-a-number", "4.5"])
    def test_a_nonsense_limit_fails_loudly_rather_than_rejecting_everything(
        self, monkeypatch, raw
    ):
        """Every limit is a ceiling compared with `>=`, so 0 or a negative silently turns it
        into "reject every sandbox request" — a total outage of the sandbox data path that no
        health check would attribute to a typo in a manifest. It must stop the pod from
        starting."""
        monkeypatch.setenv("SANDBOX_MAX_REQUESTS_PER_EXECUTION", raw)
        with pytest.raises(ValueError, match="positive integer"):
            self.budget._env_int("SANDBOX_MAX_REQUESTS_PER_EXECUTION", 1000)
