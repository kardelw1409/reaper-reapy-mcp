"""Shared fixtures for the vision tests.

Everything here is hermetic by construction: REAPER is never contacted. The
capture primitives are exercised against a REAL window (the shell's taskbar) so
that the Win32 path is not merely mocked, while the tool bodies run against a
fake host and a synthetic frame - which is what makes it possible to assert that
the composition path (capture -> encode -> Image + JSON) really happens.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="window capture is Win32 only")

#: Measured on the live host: `CSurf_OnScroll(dx, 0)` moves the view 16 px per
#: unit, and the fake host models the same law so the arithmetic under test is
#: the arithmetic that runs against REAPER.
PX_PER_SCROLL_UNIT = 16.0


class Buf:
    """Stands in for the ctypes buffer a real capture returns (``.raw``)."""

    def __init__(self, data: bytes) -> None:
        self.raw = data


def make_shot(width: int = 800, height: int = 600, *, pattern: str = "gradient",
              origin: int = 0) -> dict:
    """A synthetic captured frame with a real, decodable BGRA buffer.

    `pattern="flat"` is what a failed render looks like; `gradient` and `bars`
    are distinguishable so comparisons (similarity, shift) have something to
    measure.
    """
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            if pattern == "flat":
                red = green = blue = 40
            elif pattern == "bars":
                value = 255 if ((y // 20) % 2 == 0) else 0
                red = green = blue = value
            else:
                red = (x + origin) % 256
                green = y % 256
                blue = (x + y + origin) % 256
            row += bytes((blue, green, red, 255))
        rows.append(bytes(row))
    return {"ok": True, "rect": [10, 20, 10 + width, 20 + height],
            "size": (width, height), "buf": Buf(b"".join(rows)), "rows": height}


@pytest.fixture()
def fake_shot():
    return make_shot()


def taskbar_hwnd() -> int:
    """A real window that exists without REAPER (0 when there is none)."""
    if sys.platform != "win32":
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    for klass in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        handle = user32.FindWindowW(klass, None)
        if handle:
            return int(handle)
    return 0


class FakeHost:
    """A ReaScript API stand-in that records what was asked of it.

    It answers the calls the vision layer makes and is scriptable: `view` is the
    (start, end) it reports, `quantise` shifts a written start by that many
    seconds (which is what REAPER does), `names` maps action ids to names, so a
    re-mapped action list can be simulated.
    """

    def __init__(self, *, view=(0.0, 30.0), zoom=5.0, tempo=120.0, tracks=8,
                 length=120.0, signature=(4, 4, 120.0), quantise=0.0,
                 state_count=10, undo_label="nothing", main_hwnd="(HWND)0x1234",
                 names=None, fail=(), sources=None,
                 media_opens_after=None, resource_path=None) -> None:
        self.view = list(view)
        self.zoom = zoom
        self.tempo = tempo
        self.tracks = tracks
        self.length = length
        self.signature = signature
        self.quantise = quantise
        self.state_count = state_count
        self.undo_label = undo_label
        self.main_hwnd = main_hwnd
        self.names = dict(names or {})
        self.fail = set(fail)
        self.calls: list[str] = []
        self.writes: list[tuple] = []
        # Each entry: {"track": 0, "type": "WAVE", "name": "song.wav", "length": 0.0}.
        # `length` is what the host reports for an OPEN source, which is exactly
        # the state REAPER is in only while its window is in the foreground
        # (measured: 0.0 in the background, 89.079 s once activated).
        self.sources = [dict(entry) for entry in (sources or [])]
        for index, entry in enumerate(self.sources):
            entry.setdefault("track", 0)
            entry.setdefault("type", "WAVE")
            entry.setdefault("name", f"source_{index}.wav")
            entry.setdefault("length", 0.0)
        #: Number of `GetMediaSourceLength` reads after which the sources report a
        #: positive length - how the test scripts "REAPER opens the media a moment
        #: later". None = never.
        self.media_opens_after = media_opens_after
        self.length_reads = 0
        #: REAPER's resource directory, where `reaper.ini` lives. Measured: the call
        #: takes NO arguments through reapy and answers e.g. 'C:\\REAPER'.
        self.resource_path = resource_path

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} is configured to fail (test host)")

    # --- the calls the vision layer makes ---------------------------------
    def GetMainHwnd(self):
        self.calls.append("GetMainHwnd")
        self._maybe_fail("GetMainHwnd")
        return self.main_hwnd

    def GetSet_ArrangeView2(self, proj, is_set, a, b, start, end):
        self.calls.append("GetSet_ArrangeView2")
        self._maybe_fail("GetSet_ArrangeView2")
        if is_set:
            # The host honours the span exactly and quantises the position.
            self.view = [float(start) + self.quantise, float(start) + self.quantise
                         + (float(end) - float(start))]
            self.writes.append(("view", tuple(self.view)))
        return [0, bool(is_set), 0, 0, self.view[0], self.view[1]]

    def GetHZoomLevel(self):
        self.calls.append("GetHZoomLevel")
        return self.zoom

    def Master_GetTempo(self):
        return self.tempo

    def CountTracks(self, proj):
        return self.tracks

    def GetProjectLength(self, proj):
        return self.length

    def TimeMap_GetTimeSigAtTime(self, proj, time, numerator, denominator, bpm):
        if self.signature is None:
            raise RuntimeError("no time signature (test host)")
        num, den, value = self.signature
        return [proj, time, num, den, value]

    def GetProjectStateChangeCount(self, proj):
        return self.state_count

    def GetResourcePath(self):
        # Takes no arguments - measured on the live host, where passing a buffer
        # raised "takes 0 positional arguments but 1 was given".
        self.calls.append("GetResourcePath")
        return self.resource_path

    def Undo_CanUndo2(self, proj):
        return self.undo_label

    def kbd_getTextFromCmd(self, command, section):
        from reaper_reapy_mcp.vision.view import ACTION_SPEC

        for key, (cid, name) in ACTION_SPEC.items():
            if cid == command:
                return self.names.get(command, name)
        return None

    def Main_OnCommand(self, command, section):
        self.calls.append(f"Main_OnCommand:{command}")
        self._maybe_fail(f"Main_OnCommand:{command}")
        return True

    def CSurf_OnScroll(self, dx, dy):
        self.calls.append("CSurf_OnScroll")
        self._maybe_fail("CSurf_OnScroll")
        # The measured law of the real host: one unit is 16 pixels, so the time
        # the view moves is 16 * units / pixels-per-second.
        seconds = dx * PX_PER_SCROLL_UNIT / self.zoom
        self.view = [self.view[0] + seconds, self.view[1] + seconds]
        return 0

    # --- the media-source calls the activation path makes -------------------
    def _track_sources(self, track):
        """The sources on a 1-based track token, in item order."""
        return [entry for entry in self.sources if entry["track"] == track - 1]

    def _source_entry(self, source):
        track, item_index = source
        return self._track_sources(track)[item_index]

    def GetTrack(self, proj, index):
        self.calls.append(f"GetTrack:{index}")
        self._maybe_fail("GetTrack")
        return index + 1

    def CountTrackMediaItems(self, track):
        return len(self._track_sources(track))

    def GetTrackMediaItem(self, track, item_index):
        return (track, item_index)

    def GetMediaItemTake(self, item, take_index):
        return item

    def GetMediaItemTake_Source(self, take):
        return take

    def GetMediaSourceType(self, source, buffer, size):
        # Slot 1 carries the text - measured on the live host, where slot 0 is the
        # call's return value printed as a pointer repr (reading the wrong slot is
        # how a path once came back as '(PCM_source*)0xA5D16C0').
        return [source, self._source_entry(source)["type"], size]

    def GetMediaSourceFileName(self, source, buffer, size):
        return [source, self._source_entry(source)["name"], size]

    def GetMediaSourceLength(self, source, is_qn):
        self.length_reads += 1
        entry = self._source_entry(source)
        length = entry["length"]
        if self.media_opens_after is not None and self.length_reads > self.media_opens_after:
            length = length if length > 0 else 89.079
        return [length, is_qn]


@pytest.fixture()
def host():
    """The fake ReaScript host: 2/4 at 150.121 BPM, 16 tracks, 87.928836 s.

    It lives in conftest because pytest fixtures are per directory and a fixture
    defined inside one test module is invisible to the others - which is how two
    modules came to hold their own copies of it.
    """
    return FakeHost(view=(0.0, 30.0), tempo=150.121, signature=(2, 4, 150.121),
                    length=87.928836, zoom=13.34)


@pytest.fixture()
def server(monkeypatch, tmp_path, host):
    """A vision server with a fake host and a scripted capture.

    Returns ``(mcp, frames)``; `frames` counts captures so a test can see how many
    were taken. `host` is a fixture ARGUMENT on purpose: referring to the name
    without requesting it hands the body the fixture function object, and the
    failure then reads like a broken host
    (``'FixtureFunctionDefinition' object has no attribute 'Master_GetTempo'``)
    rather than like a test mistake.
    """
    import reaper_reapy_mcp.vision.artifacts as artifacts
    from mcp.server.fastmcp import FastMCP
    from reaper_reapy_mcp.vision import capture, tools

    monkeypatch.setenv(artifacts.VISION_DIR_ENV, str(tmp_path))
    artifacts.reset_session_dir()

    frames = {"n": 0}

    def _capture_window(hwnd, *, settle=True, tries=4, delay=0.12, auto_restore=True):
        frames["n"] += 1
        shot = make_shot(640, 400, origin=frames["n"])
        return shot, {"minimized_before": False, "auto_restored": False,
                      "focus_restored": None, "minimized_after": None,
                      "maximized": True, "visible": True,
                      "foreground_before": "0x1", "rect": list(shot["rect"]),
                      "settle": {"settled": True, "attempts": 2, "history": []},
                      "capture_ms_total": 3.0, "size": list(shot["size"]),
                      "stats": capture.image_stats(shot),
                      "looks_like_window": True}

    monkeypatch.setattr(capture, "capture_window", _capture_window)
    monkeypatch.setattr(capture, "window_rect", lambda hwnd: [0, 0, 640, 400])
    monkeypatch.setattr(capture, "is_minimized", lambda hwnd: False)
    monkeypatch.setattr(capture, "is_maximized", lambda hwnd: True)
    monkeypatch.setattr(capture, "is_visible", lambda hwnd: True)
    monkeypatch.setattr(capture, "foreground_hwnd", lambda: 1)
    monkeypatch.setattr(capture, "activate_window",
                        lambda hwnd: {"ok": True, "method": "SetForegroundWindow",
                                      "foreground_before": "0x1",
                                      "foreground_after": hex(hwnd),
                                      "elapsed_s": 0.01})
    monkeypatch.setattr(capture, "restore_foreground",
                        lambda hwnd: {"ok": True, "method": "AttachThreadInput chain",
                                      "foreground_after": hex(hwnd)})
    monkeypatch.setattr(tools, "_host", lambda: (host, 0x1234))

    mcp = FastMCP("vision-test")
    tools.setup_vision_tools(mcp)
    return mcp, frames

