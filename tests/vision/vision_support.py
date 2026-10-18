"""Unique-named access to the vision test helpers.

WHY THIS FILE EXISTS (measured 2026-10-18, and it was a real collection failure)

`tests/vision/conftest.py` and `tests/plugins/conftest.py` are two different
modules with the SAME importable name. Running the two directories in one session -

    pytest tests/vision tests/plugins

- made every `from conftest import ...` in the vision tests resolve to the PLUGINS
conftest, and five modules failed to collect:

    ImportError: cannot import name 'FakeHost' from 'conftest'
    (C:\\repos\\reaper-reapy-mcp\\tests\\plugins\\conftest.py)

Which file wins depends on the order pytest happened to import them, so the
collision is latent in EVERY combined run (a full-tree run, a CI run) even though
each directory alone passes.

THE FIX IS NOT TO MOVE THE HELPERS but to load them BY PATH, under a private module
name: `sys.path` can no longer decide which file `conftest` means, because this
module never asks for `conftest` by name. The helpers stay where a reader expects
them, next to the fixtures that use them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).with_name("conftest.py")
_SPEC = importlib.util.spec_from_file_location("vision_conftest", _PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - file always exists
    raise ImportError(f"cannot load the vision test helpers from {_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
# Registered before execution so that dataclasses/pickle-style lookups inside the
# module can find themselves by name.
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

Buf = _MODULE.Buf
FakeHost = _MODULE.FakeHost
PX_PER_SCROLL_UNIT = _MODULE.PX_PER_SCROLL_UNIT
make_shot = _MODULE.make_shot
taskbar_hwnd = _MODULE.taskbar_hwnd
windows_only = _MODULE.windows_only
REPO_ROOT = _MODULE.REPO_ROOT
SRC = _MODULE.SRC

__all__ = [
    "Buf",
    "FakeHost",
    "PX_PER_SCROLL_UNIT",
    "REPO_ROOT",
    "SRC",
    "make_shot",
    "taskbar_hwnd",
    "windows_only",
]
