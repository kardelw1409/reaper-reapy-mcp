"""Scratch lens (P23): which controls the offline fixture registry declares. Read-only."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB  # noqa: E402

reg = PB.load_registry(directory=str(REPO / "tests" / "plugins" / "fixtures" / "vital_registry"))
for row in reg.rows():
    print(
        "{:26s} {:12s} n_states={} name={}".format(
            str(row.get("state_key")), str(row.get("kind")), row.get("n_states"), row.get("name")
        )
    )
print("scales:", sorted(reg.scales().keys()))
