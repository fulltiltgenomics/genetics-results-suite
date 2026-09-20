"""TEST entrypoint. Never in the image, never in a manifest, never reachable from a pod.

It differs from url-fetcher/main.py in exactly ONE argument: `allow_loopback_origin=True`.
That allowance unlocks a loopback origin and nothing else — the metadata endpoint, RFC1918,
CGNAT and every other non-global class stay refused, which is what lets
scripts/external-inputs-proving-ground.py measure the epic's four dangerous address classes
against THIS instance and trust the answer.

It binds 127.0.0.1 by default. The allowance is supposed to be unreachable from a cluster by
construction; binding loopback means it would also be unreachable in fact.

  python3 url-fetcher/devserver.py            # the permissive instance, :8090
  python3 url-fetcher/main.py                 # the deployed configuration, for the same
                                              # harness's --fetcher-url-guarded
"""

import os
import sys

from fetch import Fetcher
from guard import DEFAULT_ALLOWED_HOSTS, Guard
from server import serve


def main():
    guard = Guard(DEFAULT_ALLOWED_HOSTS, allow_loopback_origin=True)
    print("url-fetcher: DEVELOPMENT INSTANCE — loopback origins are permitted. This "
          "construction is for the local proving ground and must never be deployed.",
          flush=True)
    return serve(Fetcher(guard),
                 host=os.environ.get("URL_FETCHER_BIND", "127.0.0.1"),
                 port=int(os.environ.get("URL_FETCHER_PORT", "8090")))


if __name__ == "__main__":
    sys.exit(main())
