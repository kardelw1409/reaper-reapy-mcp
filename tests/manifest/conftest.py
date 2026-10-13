"""Pytest bootstrap for the audiocpp-manifest offline suite (REAPER not needed).

Src-layout import without an install, mirroring tests/plugins/conftest.py.
Nothing here touches the host: manifest.py is pure Python and apply.py is only
imported (no connection happens at import time).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
