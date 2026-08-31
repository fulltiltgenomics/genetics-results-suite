import json
import os
import socket
import time

from .harness import _AliveForkServer, check, expect_request_error, make_body, sup


def test_queue(tmp):
    root = os.path.join(tmp, "queue-root")
    os.makedirs(root)
    s = sup.Supervisor(root, ready=True)
    s.forkserver = _AliveForkServer()

    def job():
        return sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)

    running = job()
    s._admit(running)
    s._await_slot(running)
    waiting = [job(), job()]
    for j in waiting:
        s._admit(j)

    code, health = s.health()
    check("queue: /health busy while running", health["busy"] is True)
    check("queue: /health queued counts only waiting", health["queued"] == 2,
          f"got {health['queued']}")
    check("queue: a busy supervisor is healthy (200)", code == 200, f"got {code}")

    expect_request_error("queue: fourth request -> 429 Busy", lambda: s._admit(job()), 429, "Busy")

    dup = sup.Job(sup.parse_execute_request(
        json.dumps(make_body(execution_id=running.req.execution_id)).encode()), None)
    expect_request_error("queue: repeated execution_id (live) -> 409",
                         lambda: s._admit(dup), 409, "DuplicateExecutionId")

    s._release(running, retain=True)
    dup2 = sup.Job(sup.parse_execute_request(
        json.dumps(make_body(execution_id=running.req.execution_id)).encode()), None)
    expect_request_error("queue: repeated execution_id (retained) -> 409",
                         lambda: s._admit(dup2), 409, "DuplicateExecutionId")

    order = []
    for _ in waiting:
        nxt = s._waiting[0]
        s._await_slot(nxt)
        order.append(nxt)
        s._release(nxt, retain=False)
    check("queue: dequeued first in, first out", order == waiting)

    # bounded wait: the 429 arrives from waiting too long, not only from depth
    slow = job()
    s._admit(slow)
    s._await_slot(slow)
    late = job()
    s._admit(late)
    original = sup.MAX_QUEUED_WAIT_S
    sup.MAX_QUEUED_WAIT_S = 0.2
    late.enqueued_at = time.monotonic()
    try:
        expect_request_error("queue: maximum wait exceeded -> 429",
                             lambda: s._await_slot(late), 429, "Busy")
    finally:
        sup.MAX_QUEUED_WAIT_S = original
    s._release(slow, retain=False)

    # a not-ready supervisor is observable: the socket binds before prewarm runs
    starting = sup.Supervisor(root, ready=False)
    code, health = starting.health()
    check("queue: not ready -> 503 status starting",
          code == 503 and health["status"] == "starting", f"got {code} {health}")
    expect_request_error("queue: not ready -> 503 NotReady on /execute",
                         lambda: starting._admit(job()), 503, "NotReady")
    starting.draining = True
    check("queue: draining -> status draining", starting.health()[1]["status"] == "draining")

    # a draining supervisor refuses with 503 NotReady
    s.draining = True
    expect_request_error("queue: draining -> 503 NotReady", lambda: s._admit(job()), 503, "NotReady")
    s.draining = False

    # expired tokens are refused at dequeue, not at accept
    expired = sup.Job(sup.parse_execute_request(
        json.dumps(make_body(exp=int(time.time()) - 5)).encode()), None)
    expect_request_error("queue: expired tokens -> 409 TokenExpired",
                         lambda: s.run(expired), 409, "TokenExpired")
    check("queue: a refused dequeue leaves no directory",
          not os.path.exists(os.path.join(root, expired.req.execution_id)))


def test_peer_gone():
    a, b = socket.socketpair()
    check("disconnect: live peer is not gone", sup.peer_gone(a) is False)
    b.close()
    check("disconnect: closed peer is detected", sup.peer_gone(a) is True)
    a.close()
