"""REAPER's media-offline setting: the real cause of a project that "looks empty".

THE SETTING (the user found it on 2026-10-18, after the behaviour had been measured
from the other side)

    Preferences -> Media -> "Set media items offline when application is not active"
    reaper.ini: offlineinact=0 (off) / =1 (on)

Measured with it ON: the mix answered `GetMediaSourceLength = 0.0`, 1 channel and
0 Hz while REAPER was in the background, its item drew NO waveform, and 1.09 s after
the window came forward the same call answered 89.079 s / 2 / 48000 - then it
unloaded again (0 of 6 sources) the moment the focus went back.

Measured with it OFF: 6 of 6 sources stay open in the background, and the frames
taken while REAPER was behind another window are PIXEL-IDENTICAL to the frames taken
while it was active (whole-window mean |delta| 0.000; the waveform ink measured
9.06 % in all three moments).

So this file pins two things: that the setting is READ correctly (and that an
unreadable one is reported as UNKNOWN, never as "off" - because "off" is the claim
that makes an empty item surprising), and that both tools carry it in their answers
so the cause is named instead of the symptom.
"""

from __future__ import annotations

import asyncio
import json

from vision_support import FakeHost

from mcp.server.fastmcp import FastMCP

from reaper_reapy_mcp.vision import capture, tools, view


def write_ini(tmp_path, body: str) -> str:
    path = tmp_path / "reaper.ini"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_setting_on_is_read_as_enabled(tmp_path):
    ini = write_ini(tmp_path, "[REAPER]\nmaxproj=10\nofflineinact=1\n")
    info = view.offline_when_inactive(None, ini_path=ini)
    assert info["enabled"] is True
    assert info["raw"] == "1"
    assert info["source"] == "reaper.ini"


def test_setting_off_is_read_as_disabled(tmp_path):
    ini = write_ini(tmp_path, "[REAPER]\nofflineinact=0\n")
    info = view.offline_when_inactive(None, ini_path=ini)
    assert info["enabled"] is False
    assert info["raw"] == "0"


def test_a_missing_key_is_unknown_not_off(tmp_path):
    """REAPER writes the key when it differs from its default, so absence is unknown.

    Reporting it as "off" would be the claim that makes an empty-looking item
    surprising, which is exactly the confusion this whole area produced.
    """
    ini = write_ini(tmp_path, "[REAPER]\nmaxproj=10\n")
    info = view.offline_when_inactive(None, ini_path=ini)
    assert info["enabled"] is None
    assert "UNKNOWN" in info["note"]


def test_an_unreadable_ini_is_unknown(tmp_path):
    info = view.offline_when_inactive(None, ini_path=str(tmp_path / "nope.ini"))
    assert info["enabled"] is None
    assert "could not be read" in info["note"]


def test_the_setting_is_named_in_the_answer_when_it_is_on(tmp_path, monkeypatch):
    """`vision_state` must name the cause, not just show the symptom."""
    import reaper_reapy_mcp.vision.artifacts as artifacts

    monkeypatch.setenv(artifacts.VISION_DIR_ENV, str(tmp_path))
    artifacts.reset_session_dir()
    ini = write_ini(tmp_path, "[REAPER]\nofflineinact=1\n")
    host = FakeHost(tracks=1, sources=[{"track": 0, "name": "song.wav", "length": 0.0}])
    monkeypatch.setattr(capture, "window_rect", lambda hwnd: [0, 0, 640, 400])
    monkeypatch.setattr(capture, "is_minimized", lambda hwnd: False)
    monkeypatch.setattr(capture, "is_maximized", lambda hwnd: True)
    monkeypatch.setattr(tools, "_host", lambda: (host, 0x1234))
    # The reader resolves `reaper.ini` through the host's resource path; pointing it
    # at the temp file is the only thing that has to be arranged. The original is
    # captured FIRST - a lambda that looked the name up again would call itself.
    original_reader = view.offline_when_inactive
    monkeypatch.setattr(view, "offline_when_inactive",
                        lambda rpr, ini_path=None: original_reader(None, ini_path=ini))

    mcp = FastMCP("vision-settings-test")
    tools.setup_vision_tools(mcp)
    content = asyncio.run(mcp.call_tool("vision_state", {}))
    if isinstance(content, tuple):
        content = content[0]
    texts = [block for block in content if getattr(block, "type", None) == "text"]
    payload = json.loads(texts[-1].text)

    assert payload["status"] == "success"
    settings = payload["media_settings"]
    assert settings["enabled"] is True
    assert "Media" in settings["setting"]
    assert "waveform" in payload["note"]
    assert "media" in payload, "the symptom and the cause have to arrive together"
