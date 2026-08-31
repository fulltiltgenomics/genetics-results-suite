import gc
import json
import os
import threading
import time

from .harness import Server, check, make_body, skip, sup


_ISOLATION_PROBE = r'''
import gc, json, os, sys, time
import collections

PAIRS = __PAIRS__          # [[label, first_half, second_half], ...]
SLEEP_S = __SLEEP_S__

def _hit(text, a, b):
    i = text.find(a)
    while i != -1:
        if text[i + len(a): i + len(a) + len(b)] == b:
            return True
        i = text.find(a, i + 1)
    return False

def _hit_bytes(blob, a, b):
    a = a.encode(); b = b.encode()
    i = blob.find(a)
    while i != -1:
        if blob[i + len(a): i + len(a) + len(b)] == b:
            return True
        i = blob.find(a, i + 1)
    return False

_SEQS = (list, tuple, set, frozenset, collections.deque)

def _harvest(roots, depth=8, budget=2000000):
    """Every string reachable from `roots`. Covers module globals, frame dicts and instance
    attributes including __slots__, which is what the bead's routes 1-3 walked by hand."""
    seen = set()
    stack = [(r, 0) for r in roots]
    n = 0
    while stack and n < budget:
        obj, d = stack.pop()
        if d > depth:
            continue
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(obj, str):
            n += 1
            yield obj
            continue
        if isinstance(obj, (bytes, bytearray)):
            n += 1
            yield bytes(obj).decode('utf-8', 'replace')
            continue
        if isinstance(obj, dict):
            for k, v in list(obj.items())[:50000]:
                stack.append((k, d + 1))
                stack.append((v, d + 1))
            continue
        if isinstance(obj, _SEQS):
            for v in list(obj)[:50000]:
                stack.append((v, d + 1))
            continue
        dd = getattr(obj, '__dict__', None)
        if isinstance(dd, dict):
            stack.append((dd, d + 1))
        for slot in getattr(type(obj), '__slots__', ()) or ():
            try:
                stack.append((getattr(obj, slot), d + 1))
            except Exception:
                pass

def route_module_global():
    roots = [m for m in list(sys.modules.values()) if getattr(m, 'SUPERVISOR', None) is not None]
    roots = [m.SUPERVISOR for m in roots] + [getattr(m, '__dict__', {}) for m in roots]
    return _harvest(roots)

def route_frames():
    roots = []
    f = sys._getframe()
    while f is not None:
        roots.append(f.f_locals)
        roots.append(f.f_globals)
        f = f.f_back
    return _harvest(roots, depth=6)

def route_gc():
    for obj in gc.get_objects():
        for ref in gc.get_referents(obj):
            if isinstance(ref, str):
                yield ref
            elif isinstance(ref, (bytes, bytearray)):
                yield bytes(ref).decode('utf-8', 'replace')

def scan_refs(route, out):
    for text in route:
        for label, a, b in PAIRS:
            if label in out:
                continue
            if _hit(text, a, b):
                out.add(label)

def scan_mem(out):
    """The route that decided the design: the raw address space, which no amount of dropping
    references can clean because freed strings stay in the arenas COW hands over.

    Its findings are kept SEPARATE from the reference routes'. Folding them together lets a
    dead memory scan hide behind a live reference hit, and this is the route the bead says
    decides the design — it has to be shown working on its own.
    """
    try:
        lines = open('/proc/self/maps').read().splitlines()
        mem = os.open('/proc/self/mem', os.O_RDONLY)
    except OSError as exc:
        return 'unavailable: %s' % exc
    scanned = 0
    try:
        for line in lines:
            parts = line.split()
            if len(parts) < 2 or 'r' not in parts[1]:
                continue
            path = parts[5] if len(parts) > 5 else ''
            if path in ('[vvar]', '[vdso]', '[vsyscall]', '[vvar_vclock]'):
                continue
            lo, _, hi = parts[0].partition('-')
            try:
                lo = int(lo, 16); hi = int(hi, 16)
            except ValueError:
                continue
            size = hi - lo
            if size <= 0 or size > 64 * 1024 * 1024 or scanned > 768 * 1024 * 1024:
                continue
            try:
                os.lseek(mem, lo, os.SEEK_SET)
                blob = os.read(mem, size)
            except OSError:
                continue
            scanned += len(blob)
            for label, a, b in PAIRS:
                if label not in out and _hit_bytes(blob, a, b):
                    out.add(label)
    finally:
        os.close(mem)
    return scanned

result = {}
for phase in ('released', 'queued'):
    if phase == 'queued':
        time.sleep(SLEEP_S)
    found = set()
    scan_refs(route_module_global(), found)
    scan_refs(route_frames(), found)
    scan_refs(route_gc(), found)
    in_mem = set()
    mem = scan_mem(in_mem)
    result[phase] = {'found': sorted(found | in_mem), 'ref_found': sorted(found),
                     'mem_found': sorted(in_mem),
                     'mem': mem if isinstance(mem, str) else 'ok'}
print('PROBERESULT ' + json.dumps(result))
'''


def _pair(value):
    """A needle as two halves, so the probe never holds the whole thing."""
    cut = len(value) // 2
    return [value[:cut], value[cut:]]


def test_isolation(tmp):
    root = os.path.join(tmp, "isolation")
    os.makedirs(root)

    # PLANTED BEFORE THE SERVER IS BUILT, and that ordering is the control. bring_up() forks
    # the fork server, so anything set on the module now is in the fork server's inherited
    # pages and every child must be able to see it.
    control = "FORKSRVCTL" + os.urandom(16).hex().upper()
    sup.ISOLATION_TEST_CONTROL = control

    # The second control: a string dropped and gc.collect()-ed BEFORE the fork, measured still
    # recoverable in the child. It is what proves the raw scan reads FREED arenas and not merely
    # live objects, and therefore what makes "reference-based clearing cannot work" a
    # measurement rather than a claim. Reported rather than required, because an arena can
    # legitimately be reused or returned between the drop and the scan.
    # 4 KiB of padding so the allocation goes to malloc rather than a pymalloc pool: a 44-byte
    # string lands in a size class the supervisor's own startup reuses within microseconds.
    freed = "FORKSRVFREED" + os.urandom(16).hex().upper() + "." * 4096
    freed_pair = _pair(freed[:44])   # the halves survive; the whole string must not
    sup.ISOLATION_TEST_FREED = freed
    del sup.ISOLATION_TEST_FREED
    del freed
    gc.collect()

    server = Server(root)
    try:
        # -- victim 1: runs to completion and is released before the probe starts.
        v1 = os.urandom(12).hex().upper()
        body1 = make_body(code=f"x = 'VICTIMCODE{v1}'\nprint('victim one')\n",
                          user=f"v1-{v1[:8]}@b.c", session_id=f"sess-{v1}")
        status, _, _ = server.request("POST", "/execute", body1)
        check("isolation: the released victim executed", status == 200, f"got {status}")

        # -- victim 2 will be QUEUED behind the probe. Its markers are known now so the probe
        # can carry them; the request itself is sent after the probe is running.
        v2 = os.urandom(12).hex().upper()
        body2 = make_body(code=f"y = 'VICTIMCODE{v2}'\nprint('victim two')\n",
                          user=f"v2-{v2[:8]}@b.c", session_id=f"sess-{v2}")

        pairs = [
            ["control-forkserver", *_pair(control)],
            ["control-freed", *freed_pair],
            ["released-token", *_pair(body1["tokens"]["db-api"].split(".")[1])],
            ["released-code", *_pair("VICTIMCODE" + v1)],
            ["released-session", *_pair("sess-" + v1)],
            ["queued-token", *_pair(body2["tokens"]["db-api"].split(".")[1])],
            ["queued-code", *_pair("VICTIMCODE" + v2)],
            ["queued-session", *_pair("sess-" + v2)],
        ]
        probe_code = (_ISOLATION_PROBE
                      .replace("__PAIRS__", json.dumps(pairs))
                      .replace("__SLEEP_S__", "2.0"))
        probe_body = make_body(code=probe_code, user="probe@b.c", session_id="sess-probe")
        probe_body["timeout_s"] = 60

        box = {}
        t = threading.Thread(
            target=lambda: box.update(zip(("status", "retry", "body"),
                                          server.request("POST", "/execute", probe_body))),
            daemon=True)
        t.start()
        # Long enough for the probe to be the running execution, short enough that victim 2 is
        # still queued when the probe's second phase scans.
        time.sleep(0.6)
        box2 = {}
        t2 = threading.Thread(
            target=lambda: box2.update(zip(("status", "retry", "body"),
                                           server.request("POST", "/execute", body2))),
            daemon=True)
        t2.start()
        t.join(120)
        t2.join(120)

        body = box.get("body") or {}
        check("isolation: the probe execution completed",
              box.get("status") == 200 and body.get("status") == "ok",
              f"got {box.get('status')} {str(body)[:300]}")
        check("isolation: victim two was queued behind the probe and then ran",
              box2.get("status") == 200, f"got {box2.get('status')}")

        line = ""
        for candidate in (body.get("output") or "").splitlines():
            if candidate.startswith("PROBERESULT "):
                line = candidate[len("PROBERESULT "):]
        try:
            result = json.loads(line)
        except Exception:
            result = None
        check("isolation: the probe reported a result",
              isinstance(result, dict) and set(result) == {"released", "queued"},
              f"output was {str(body.get('output'))[:400]}")
        if not isinstance(result, dict):
            return

        for phase in ("released", "queued"):
            found = set(result[phase]["found"])
            in_mem = set(result[phase]["mem_found"])
            in_refs = set(result[phase].get("ref_found", ()))
            check(f"isolation [{phase}]: the positive control IS reachable, so the search works",
                  "control-forkserver" in found, f"found {sorted(found)}")
            # Separately from the mem scan: `found` is the union, and the union hid a dead
            # search. Sabotaging scan_refs — which kills the module-global, frame-walk and gc
            # routes at once — left the suite green, because the mem hit alone satisfied the
            # check above. Three of the four advertised routes had no positive control at all.
            check(f"isolation [{phase}]: the reference routes (module global, frame walk, gc) "
                  f"reach the positive control on their own",
                  "control-forkserver" in in_refs, f"the reference routes found {sorted(in_refs)}")
            check(f"isolation [{phase}]: the raw /proc/self/mem scan reaches the fork "
                  f"server's inherited pages",
                  result[phase]["mem"] == "ok" and "control-forkserver" in in_mem,
                  f"mem said {result[phase]['mem']}, found {sorted(in_mem)}")
            if "control-freed" in in_mem:
                check(f"isolation [{phase}]: the memory scan recovers a string FREED before "
                      f"the fork, which is why no reference-clearing fix could have worked",
                      True)
            else:
                skip(f"isolation [{phase}]: the freed-string control",
                     "its arena was reused or returned before the scan; the live-object "
                     "control above still proves the scan reads inherited pages")
            leaked = sorted(found - {"control-forkserver", "control-freed"})
            check(f"isolation [{phase}]: no other execution's token, code or session id "
                  f"is reachable from the child", not leaked, f"LEAKED {leaked}")
    finally:
        server.close()
