"""Pytest bootstrap for the offline plugin-layer suite.

The plugin layer is imported from the source tree (`src` layout) without
requiring an install, and nothing here may import a host module or connect to
anything - the whole suite runs with REAPER closed (12 §1).

**Live-write gate (P5, and until P8 authorises otherwise).** The plugin suite
refuses any write issued through the *real* bridge: the autouse fixture below
replaces `ReaperHostBridge.write_params` / `write_state_slice` with a loud
refusal. A test that genuinely needs a live mutation must carry the `live_write`
marker - and no test does before P8. This makes "no live mutation in this phase"
a mechanical property of the suite rather than a promise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: Offline tests are hermetic about the host-calibration artifact: the environment
#: override points at a file that does not exist, so a machine whose user-local
#: preview directory holds a real artifact cannot change an offline result. Tests
#: that exercise calibration pass an explicit `calibration_path=` (the pinned
#: fixture) or their own environment mapping. The measured enum-boundary artifact
#: gets the same treatment for the same reason.
_CALIBRATION_OFF = str(Path(__file__).resolve().parent / "fixtures" / "no_calibration_configured.json")


@pytest.fixture(autouse=True)
def _pin_offline_calibration(monkeypatch):
    monkeypatch.setenv("REAPER_MCP_VITAL_CALIBRATION", _CALIBRATION_OFF)
    monkeypatch.setenv("REAPER_MCP_VITAL_ENUM_BOUNDARIES", _CALIBRATION_OFF)


@pytest.fixture(autouse=True)
def _pin_offline_log_dir(tmp_path_factory, monkeypatch):
    """Keep the run log out of the user's real log directory.

    The layer writes one JSON line per tool call and per write unit (so a long call is
    visible while it runs). Hermetic means the suite writes those lines into a temporary
    directory instead of `%LOCALAPPDATA%`: an offline test pass must not touch - or
    sweep - the logs of a real session.
    """
    monkeypatch.setenv("REAPER_MCP_LOG_DIR", str(tmp_path_factory.mktemp("run-logs")))

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
HERE = Path(__file__).resolve().parent
for _path in (SRC, HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

#: Marker that lifts the live-write refusal (P8 and later only).
LIVE_WRITE_MARKER = "live_write"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{LIVE_WRITE_MARKER}: allow a genuine live host write "
        "(no test may carry it before the P8 gate is authorised)",
    )


@pytest.fixture(autouse=True)
def refuse_live_writes(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Refuse every write attempt made through the real REAPER bridge."""
    from reaper_reapy_mcp.plugins.host import reaper as host_reaper

    if request.node.get_closest_marker(LIVE_WRITE_MARKER):
        yield
        return

    def _refuse(self, *args, **kwargs):  # noqa: ANN001 - the replaced verb's shape
        raise AssertionError(
            "the plugin test suite attempted a live host write through "
            "ReaperHostBridge; live mutation is gated to P8 (a test that really "
            f"needs one must carry the {LIVE_WRITE_MARKER!r} marker)"
        )

    monkeypatch.setattr(host_reaper.ReaperHostBridge, "write_params", _refuse)
    monkeypatch.setattr(host_reaper.ReaperHostBridge, "write_state_slice", _refuse)
    yield
