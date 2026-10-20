"""Pytest bootstrap for the RPP builder offline suite (REAPER not needed).

Src-layout import without an install, mirroring tests/manifest/conftest.py.
Nothing here touches the host: the whole rpp_builder package is pure Python.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
