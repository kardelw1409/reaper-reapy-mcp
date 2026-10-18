"""The activation path: REAPER does not load a project's media in the background.

THE FINDING THIS PINS (user's observation 2026-10-18, then measured)
  With the REAPER window in the background, a freshly loaded project's sources are
  NOT open: `GetMediaSourceLength` answered **0.0 s**, 1 channel and 0 Hz for a mix
  whose file was on disk and named by the project - and the item drew no waveform.
  **1.09 s after the window was brought to the foreground** the same call answered
  **89.079 s / 2 / 48000**. So an empty-looking screenshot is not necessarily a
  capture fault, and a capture that must be faithful has to activate first and then
  wait for the HOST to say the media is there.

WHAT IS TESTED HERE
  * the reading itself: a source counts as open only when it reports a positive
    length, MIDI takes never hold the wait up, and the pending names are reported;
  * the WAIT is driven by the host's answer, not by a sleep - proven by a host that
    opens its sources only on a later read (and by one that never does, where the
    answer must say so instead of claiming success);
  * an unreadable project is reported as UNKNOWN, never as "nothing is open";
  * the tools carry the whole story in the `activation` block, and a picture taken
    while the media is not loaded carries a WARNING - because a model reading the
    image alone cannot know whether an empty item is empty or merely not loaded.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from vision_support import FakeHost, make_shot

from mcp.server.fastmcp import FastMCP

from reaper_reapy_mcp.vision import capture, tools, view


def build_server(monkeypatch, tmp_path, host, *, activate_ok=True, restore=None,
                 already_front=False, offline_when_inactive=1):
    """A vision server whose capture is scripted and whose host is `host`.

    A `reaper.ini` is written into `tmp_path` and the host reports it as its
    resource directory, so the REAL setting reader runs: the whole point of this
    area is that REAPER's *Set media items offline when application is not active*
    setting decides whether the media is loaded while the window is in the
    background (measured: 0 of 6 sources open with it ON, 6 of 6 with it OFF).
    """
    import reaper_reapy_mcp.vision.artifacts as artifacts

    monkeypatch.setenv(artifacts.VISION_DIR_ENV, str(tmp_path))
    artifacts.reset_session_dir()
    (tmp_path / "reaper.ini").write_text(
        f"[REAPER]\nofflineinact={offline_when_inactive}\n", encoding="utf-8")
    host.resource_path = str(tmp_path)

    def _capture_window(hwnd, *, settle=True, tries=4, delay=0.12, auto_restore=True):
        shot = make_shot(640, 400)
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
    def _activate_window(hwnd):
        if not activate_ok:
            return {"ok": False, "method": "FAILED", "foreground_before": "0x1",
                    "foreground_after": "0x1", "elapsed_s": 0.02,
                    "hint": "foreground lock"}
        if already_front:
            return {"ok": True, "method": "already foreground",
                    "foreground_before": hex(hwnd), "foreground_after": hex(hwnd),
                    "elapsed_s": 0.0}
        return {"ok": True, "method": "AttachThreadInput chain",
                "foreground_before": "0x1", "foreground_after": hex(hwnd),
                "elapsed_s": 0.02}

    monkeypatch.setattr(capture, "activate_window", _activate_window)
    # The focus restore is a host-side effect with its own measured behaviour; it
    # is scripted here so a test can assert WHETHER it was asked for and WHAT the
    # answer recorded.
    restore_calls: list[int] = []
    #: The ORDER of the two side effects. Measured, it is not cosmetic: REAPER
    #: unloads its media the moment it loses the foreground, so a restore that
    #: happens BEFORE the capture would shoot the unloaded state.
    events: list[str] = []

    def _capture_window_ordered(hwnd, **kwargs):
        events.append("capture")
        return _capture_window(hwnd, **kwargs)

    def _restore_foreground(hwnd):
        restore_calls.append(hwnd)
        events.append("restore")
        # MEASURED BEHAVIOUR BEING MODELLED: REAPER unloads a project's media the
        # moment it loses the foreground (6 of 6 sources open while active, 0 of 6
        # right after the restore). Without this the test would never exercise the
        # state the user is looking at a second after a capture.
        for entry in host.sources:
            entry["length"] = 0.0
        host.media_opens_after = None
        if restore is not None:
            return dict(restore)
        return {"ok": True, "method": "AttachThreadInput chain",
                "foreground_after": hex(hwnd)}

    monkeypatch.setattr(capture, "capture_window", _capture_window_ordered)
    monkeypatch.setattr(capture, "restore_foreground", _restore_foreground)
    monkeypatch.setattr(tools, "_host", lambda: (host, 0x1234))

    mcp = FastMCP("vision-activate-test")
    tools.setup_vision_tools(mcp)
    return mcp, restore_calls, events


def call(mcp, name: str, arguments: dict | None = None):
    content = asyncio.run(mcp.call_tool(name, arguments or {}))
    if isinstance(content, tuple):
        content = content[0]
    images = [block for block in content if getattr(block, "type", None) == "image"]
    texts = [block for block in content if getattr(block, "type", None) == "text"]
    return images, (json.loads(texts[-1].text) if texts else None)


def mix_host(**kwargs):
    """A host whose single WAVE source is NOT open - the measured background state."""
    defaults = dict(view=(0.0, 16.0), tempo=150.121, tracks=1, length=87.928836,
                    sources=[{"track": 0, "name": "song.wav", "length": 0.0}])
    defaults.update(kwargs)
    return FakeHost(**defaults)


# --- the reading ------------------------------------------------------------

def test_media_sources_counts_only_what_the_host_reports_as_open():
    host = mix_host()
    state = view.media_sources(host)
    assert state["audio_sources"] == 1
    assert state["open"] == 0
    assert state["all_open"] is False
    assert state["pending"] == ["song.wav"]


def test_media_sources_ignores_midi_and_finds_the_rest_open():
    host = FakeHost(tracks=2, sources=[
        {"track": 0, "name": "song.wav", "length": 89.079},
        {"track": 1, "name": "bass.mid", "type": "MIDI"},
    ])
    state = view.media_sources(host)
    assert state["midi"] == 1
    assert state["audio_sources"] == 1 and state["open"] == 1
    assert state["all_open"] is True
    assert state["pending"] == []


def test_media_sources_is_unknown_when_the_project_cannot_be_read():
    class Broken:
        def CountTracks(self, proj):
            raise RuntimeError("no project here")

    assert view.media_sources(Broken()) is None


# --- the wait ---------------------------------------------------------------

def test_wait_for_media_waits_until_the_host_opens_the_sources():
    # `media_opens_after=1` models the measured behaviour: the first read reports
    # 0.0 s, a later one reports the real length. The wait must be driven by that
    # answer, so it has to poll at least twice.
    host = mix_host(media_opens_after=1)
    result = view.wait_for_media(host, timeout=5.0, interval=0.02)
    assert result["ready"] is True
    assert result["timed_out"] is False
    assert result["open"] == 1
    assert host.length_reads >= 2
    assert "note" not in result


def test_wait_for_media_gives_up_and_says_what_is_missing():
    host = mix_host()                      # never opens
    result = view.wait_for_media(host, timeout=0.15, interval=0.02)
    assert result["ready"] is False
    assert result["timed_out"] is True
    assert result["open"] == 0 and result["audio_sources"] == 1
    assert "not loaded yet" in result["note"]
    assert result["waited_s"] >= 0.15


def test_wait_for_media_reports_unknown_rather_than_open():
    class Broken:
        def CountTracks(self, proj):
            raise RuntimeError("no project here")

    result = view.wait_for_media(Broken(), timeout=0.05, interval=0.01)
    assert result["ready"] is None         # UNKNOWN, not "nothing is open"
    assert "UNKNOWN" in result["note"]


def test_wait_for_media_returns_at_once_when_there_is_nothing_to_load():
    host = FakeHost(tracks=1, sources=[{"track": 0, "type": "MIDI", "name": "x.mid"}])
    result = view.wait_for_media(host, timeout=5.0, interval=0.02)
    assert result["ready"] is True
    assert result["waited_s"] < 0.5


# --- the tools --------------------------------------------------------------

def test_vision_shot_activate_reports_the_whole_activation_story(monkeypatch, tmp_path):
    host = mix_host(media_opens_after=1)
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    images, payload = call(mcp, "vision_shot", {"activate": True, "activate_timeout": 2.0})

    assert payload["status"] == "success"
    assert images, "the image is still the product"
    activation = payload["activation"]
    assert activation["ok"] is True
    assert activation["method"] == "AttachThreadInput chain"
    assert activation["media_before"]["open"] == 0            # it WAS not loaded
    assert activation["media_after"]["ready"] is True         # and then it was
    assert activation["media_after"]["open"] == 1
    assert payload["state_change_delta"] == 0
    # No warning: the media really is loaded, so an empty item would be a fact.
    assert "warning" not in activation


def test_vision_shot_warns_when_the_media_never_loads(monkeypatch, tmp_path):
    host = mix_host()                       # sources stay closed
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_shot",
                            {"activate": True, "activate_timeout": 0.05})

    assert payload["status"] == "success"
    activation = payload["activation"]
    assert activation["media_after"]["ready"] is False
    assert activation["media_after"]["timed_out"] is True
    assert activation["media_after"]["pending"] == ["song.wav"]
    # The wait may not overshoot the budget it was given.
    assert activation["media_after"]["waited_s"] <= 0.15
    assert "items without a waveform" in activation["warning"]


def test_vision_shot_warns_when_activation_itself_fails(monkeypatch, tmp_path):
    host = mix_host()
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host,
                                                activate_ok=False)
    _images, payload = call(mcp, "vision_shot", {"activate": True})

    activation = payload["activation"]
    assert activation["ok"] is False
    # The primitive's own reason is kept, and the CONSEQUENCE is stated too.
    assert activation.get("hint")
    assert "foreground" in activation["warning"]
    # The capture itself is NOT turned into an error: the frame is valid.
    assert payload["status"] == "success"


def test_vision_shot_without_activate_pays_nothing(monkeypatch, tmp_path):
    host = mix_host()
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_shot", {})

    assert "activation" not in payload
    assert host.length_reads == 0, ("no media call may be made unless activation "
                                    "was asked for")


def test_vision_scan_activates_once_before_the_tiles(monkeypatch, tmp_path):
    host = mix_host(media_opens_after=1)
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_scan",
                            {"span_seconds": 8.0, "end": 16.0, "activate": True,
                             "activate_timeout": 2.0})

    assert payload["status"] == "success"
    assert payload["activation"]["media_after"]["ready"] is True
    assert payload["tile_count"] >= 1
    assert payload["state_change_delta"] == 0
    assert host.length_reads >= 2


def test_activate_is_not_silently_ignored_by_either_tool(monkeypatch, tmp_path):
    """A parameter the transport drops is the failure mode this guards against."""
    host = mix_host(media_opens_after=1)
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    for name, arguments in (("vision_shot", {"activate": True, "activate_timeout": 1.0}),
                            ("vision_scan", {"activate": True, "activate_timeout": 1.0,
                                             "span_seconds": 8.0, "end": 16.0})):
        _images, payload = call(mcp, name, arguments)
        assert payload.get("activation", {}).get("requested") is True, name


# --- the foreground comes BACK (the user's requirement) ----------------------

def test_focus_is_given_back_to_the_window_that_had_it(monkeypatch, tmp_path):
    """Activating REAPER must not leave the user's screen on REAPER.

    Their words, 2026-10-18: they may be working on something important, and the
    capture would land them in REAPER. So the window that held the foreground
    before the call is put back - and the answer says which window and how.
    """
    host = mix_host(media_opens_after=1)
    mcp, restore_calls, events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_shot",
                            {"activate": True, "activate_timeout": 2.0})

    activation = payload["activation"]
    assert restore_calls == [1], ("the previous foreground window (0x1) must be "
                                 "handed back exactly once")
    restored = activation["focus_restored"]
    assert restored["ok"] is True
    assert restored["requested"] == "0x1"
    assert restored["foreground_after"] == "0x1"
    assert activation["restore_focus_requested"] is True
    # THE ORDER, measured: the frame is taken while REAPER still holds the
    # foreground, because losing it unloads the media again (0 of 6 sources, and
    # the waveform gone, immediately after the restore).
    assert events[-1] == "restore", events
    assert "capture" in events[:-1], events


def test_the_answer_says_the_media_unloads_when_the_focus_goes_back(monkeypatch,
                                                                   tmp_path):
    """The user's own report, turned into a stated fact.

    They saw the project's tracks unloaded while REAPER was open. This is the same
    state a capture leaves behind: returning the focus makes REAPER unload its
    media. That is reported (`media_after_restore` + a note naming the remedy),
    because otherwise the next question is "why is my project empty?".
    """
    host = mix_host(media_opens_after=1)
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_shot", {"activate": True})

    activation = payload["activation"]
    assert activation["media_after"]["open"] == 1          # loaded for the frame
    after = activation["media_after_restore"]
    assert after is not None, ("the state after the restore must be measured, not "
                               "assumed")
    assert after["open"] == 0 and after["audio_sources"] == 1
    # ... and the CAUSE is named, not just the symptom: the setting that makes
    # REAPER close the media, plus both remedies.
    assert activation["media_settings"]["offline_when_inactive"] is True
    assert "Preferences -> Media" in activation["note"]
    assert "restore_focus=false" in activation["note"]


def test_with_the_setting_off_nothing_unloads_and_the_note_stays_away(monkeypatch,
                                                                     tmp_path):
    """The user's fix, pinned: setting off = focus no longer matters.

    Measured live: `offlineinact=0` keeps 6 of 6 sources open in the background, and
    frames taken there are pixel-identical to frames taken while REAPER is active.
    So no note may claim a problem that no longer exists.
    """
    host = mix_host(media_opens_after=1)
    mcp, _restore_calls, _events = build_server(monkeypatch, tmp_path, host,
                                                offline_when_inactive=0)
    _images, payload = call(mcp, "vision_shot", {"activate": True})

    activation = payload["activation"]
    assert activation["media_settings"]["offline_when_inactive"] is False
    assert "note" not in activation
    assert "warning" not in activation


def test_focus_restore_reports_failure_instead_of_claiming_success(monkeypatch,
                                                                   tmp_path):
    host = mix_host(media_opens_after=1)
    mcp, restore_calls, _events = build_server(
        monkeypatch, tmp_path, host,
        restore={"ok": False, "method": "FAILED", "foreground_after": "0x1234",
                 "hint": "Windows refused to return the foreground"})
    _images, payload = call(mcp, "vision_shot", {"activate": True})

    restored = payload["activation"]["focus_restored"]
    assert restored["ok"] is False
    assert restored["method"] == "FAILED"
    assert "refused" in restored["hint"]
    # A failed restore is not a failed capture, and it is not hidden either.
    assert payload["status"] == "success"


def test_restore_focus_false_leaves_reaper_in_front(monkeypatch, tmp_path):
    host = mix_host(media_opens_after=1)
    mcp, restore_calls, _events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_shot",
                            {"activate": True, "restore_focus": False})

    assert restore_calls == [], "the caller asked for REAPER to stay in front"
    activation = payload["activation"]
    assert activation["restore_focus_requested"] is False
    assert "skipped" in activation["focus_restored"]["method"]
    # REAPER never lost the foreground, so nothing was unloaded and no round trip
    # was spent reading a state that cannot have changed.
    assert activation["media_after_restore"] is None
    assert "note" not in activation


def test_nothing_is_restored_when_reaper_already_had_the_foreground(monkeypatch,
                                                                    tmp_path):
    host = mix_host(media_opens_after=1)
    mcp, restore_calls, _events = build_server(monkeypatch, tmp_path, host,
                                               already_front=True)
    _images, payload = call(mcp, "vision_shot", {"activate": True})

    assert restore_calls == []
    activation = payload["activation"]
    assert activation["method"] == "already foreground"
    assert "not needed" in activation["focus_restored"]["method"]
    assert activation["media_after_restore"] is None
    # The media is still waited for: the user already had REAPER in front, but the
    # project's sources may still be loading.
    assert activation["media_after"]["ready"] is True


def test_scan_returns_the_foreground_too_and_only_after_the_tiles(monkeypatch,
                                                                 tmp_path):
    host = mix_host(media_opens_after=1)
    mcp, restore_calls, events = build_server(monkeypatch, tmp_path, host)
    _images, payload = call(mcp, "vision_scan",
                            {"span_seconds": 8.0, "end": 16.0, "activate": True,
                             "activate_timeout": 2.0})

    assert restore_calls == [1]
    assert payload["activation"]["focus_restored"]["ok"] is True
    # Every tile was taken before the focus went back: one restore, and it is the
    # last event of the run.
    assert events.count("restore") == 1, events
    assert events[-1] == "restore", events
    assert events.count("capture") == payload["tile_count"], events
