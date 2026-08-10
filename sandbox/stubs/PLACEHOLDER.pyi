# PLACEHOLDER — not the real SDK type stubs
#
# This file exists only so the sandbox image has something to COPY into
# /genetics/sdk/. It is not generated content and describes no API.
#
# genetics-results-suite-4h6.13 owns generation and must replace it.
#
# Contract for 4h6.13:
#
#   - Write generated .pyi stubs for the genetics SDK into this directory,
#     sandbox/stubs/. Mirror the SDK package layout (genetics_mcp_server/sdk/*.pyi)
#     if the stubs are per-module; the image copies the tree verbatim.
#   - The image copies sandbox/stubs/ to /genetics/sdk/, owned 65532:65532, read-only
#     at runtime.
#   - The path is exported as GENETICS_STUBS_DIR. Read that rather than hardcoding.
#   - /genetics/sdk/ is NOT on PYTHONPATH and must not be: it holds stubs for the
#     model to read, while the importable SDK is the real package installed in
#     /opt/venv from genetics-mcp-server. Two copies of the same names on sys.path
#     would be a silent shadowing hazard.
#   - Delete this file in the same change that adds the generated output; the
#     directory must not be left empty.
