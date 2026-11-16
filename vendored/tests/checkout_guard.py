"""Refuses a test run that could import another checkout's source and report green.

The services are not installed: their code is a namespace package (no `__init__.py`) found
on sys.path, and each repo's tests put their own tree first. A namespace package MERGES
every matching directory on sys.path, though, so `PYTHONPATH=<other checkout>` adds that
tree's directory to the package's `__path__`. Modules present there but not here then
import silently from the other checkout, and a deletion made here is invisible.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest


def refuse_foreign_namespace_package(config, package: str, unset_note: str = "") -> None:
    """Abort the run if `package` spans a directory outside pytest's rootdir.

    For `pytest_configure`, so it stops the run before collection rather than surfacing as
    a confusing test failure. Importing a namespace package runs no code.
    """
    rootdir = Path(config.rootpath).resolve()
    paths = importlib.import_module(package).__path__
    outside = [p for p in paths if not Path(p).resolve().is_relative_to(rootdir)]
    if outside:
        raise pytest.UsageError(
            f"the `{package}` namespace package spans directories OUTSIDE the tree pytest "
            "is testing, so this run can import another checkout's source and report green.\n"
            "    outside this tree : " + ", ".join(outside) + "\n"
            f"    pytest rootdir    : {rootdir}\n"
            f"    interpreter       : {sys.executable}\n"
            f"    PYTHONPATH        : {os.environ.get('PYTHONPATH', '(unset)')}\n"
            f"Almost always PYTHONPATH points at another checkout. Fix, from {rootdir}:\n"
            f"    unset PYTHONPATH{unset_note}\n"
            "    uv sync --extra dev\n"
            f'    uv run python -c "import {package}, sys; print(list({package}.__path__)); '
            'print(sys.executable)"\n'
            f"Every printed path must be under {rootdir}."
        )
