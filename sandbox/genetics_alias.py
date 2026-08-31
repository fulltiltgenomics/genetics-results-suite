"""Installed into the sandbox venv as `genetics.py`, so `import genetics` works.

The name is the one the contract already promises: run_analysis's description,
list_capabilities' module enum, the shipped stub file name and the schema README all call
this package `genetics`, while the importable path was `genetics_mcp_server.sdk` and nothing
reachable from inside an execution said so. Measured consequence: every session opened with
`import genetics` -> ModuleNotFoundError, then a second wrong guess and three pkgutil probes
before the first line of real work.

sys.modules aliasing, not `from ... import *`: a star-import would create a SECOND module
object with a second copy of the SDK's client state, so `genetics.configure(...)` and
`genetics_mcp_server.sdk.configure(...)` would configure different things. Rebinding
sys.modules[__name__] makes the two names one object.

Sandbox only, and deliberately not shipped from the genetics-mcp-server wheel: `genetics` is
a very generic top-level name, and chat-backend and mcp-server install the same package.
"""

import sys

from genetics_mcp_server import sdk as _sdk

sys.modules[__name__] = _sdk
