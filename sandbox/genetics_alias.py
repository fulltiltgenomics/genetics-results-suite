"""Installed into the sandbox venv as `genetics.py`, so `import genetics` works.

THE NAME IS NOT A CONVENIENCE, IT IS THE ONE THE CONTRACT ALREADY PROMISES. Every surface
a script's author can read calls this package `genetics`: `run_analysis`'s description
("write the script against the `genetics` SDK"), `list_capabilities`' module enum
('genetics', 'client', 'errors'), the shipped stub file name (`sdk/genetics.pyi`) and the
schema README ("query them with `genetics.sql(...)`"). The importable path was
`genetics_mcp_server.sdk` and nothing reachable from inside an execution said so —
`list_capabilities` deliberately strips module docstrings, which is where the real import
line lived. MEASURED consequence (genetics-results-suite-706): every session opened with
`import genetics` -> ModuleNotFoundError, a second wrong guess, then three `pkgutil`
probes, six executions before the first line of real work.

sys.modules ALIASING, NOT `from ... import *`. A star-import would create a SECOND module
object holding a second copy of the SDK's client state, so `genetics.configure(...)` and
`genetics_mcp_server.sdk.configure(...)` would configure different things and the
per-execution credential would be attached by one and not the other. Rebinding
`sys.modules[__name__]` makes the two names one object: the import system re-reads
sys.modules after executing a module, so `import genetics`, `from genetics import sql` and
`genetics.sql` all resolve to the real package.

SANDBOX ONLY, and deliberately not shipped from the genetics-mcp-server wheel. `genetics`
is a very generic top-level name, and chat-backend and mcp-server install the same package;
claiming it in every environment to fix a promise the SANDBOX makes would be a namespace
land-grab well outside the problem. `sandbox/Dockerfile` copies this file, so the alias
exists exactly where the docs that promise it are read.
"""

import sys

from genetics_mcp_server import sdk as _sdk

sys.modules[__name__] = _sdk
