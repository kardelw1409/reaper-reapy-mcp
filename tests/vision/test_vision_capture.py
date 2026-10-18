"""The capture primitives: what the image IS, and what it must refuse.

Two of these tests exist because the failure they catch was measured rather than
imagined:

* `GetForegroundWindow` is declared with a pointer restype, so a zero answer
  arrives as **None** and `int()` on it raised \"int() argument must be a string
  ... not 'NoneType'\" - an otherwise complete `vision state` answer died on a
  detail it never needed. The first test pins the parsing that survives it.
* a minimized window renders a 237x39 / 4-colour artefact. Handing that to an
  agent as a screenshot would make it reason about a fragment of a title bar, so
  `looks_like_window` decides, and the artefact signature is pinned here.

The last test captures a REAL window (the shell's taskbar) so the Win32 path is
not only ever mocked; it skips when no such window exists.
"""

from __future__ import annotations

import pytest

from vision_support import make_shot, taskbar_hwnd, windows_only

from reaper_reapy_mcp.vision import capture


def test_hwnd_parsing_survives_every_shape_the_host_and_ctypes_produce():
    assert capture.hwnd_from("(HWND)0x00000000000107B0") == 0x107B0
    assert capture.hwnd_from("0x1f4") == 0x1F4
    assert capture.hwnd_from(1234) == 1234
    assert capture.hwnd_from("(HWND)0x0") == 0
    with pytest.raises(ValueError) as none_error:
        capture.hwnd_from(None)
    assert "no window handle" in str(none_error.value)
    with pytest.raises(ValueError):
        capture.hwnd_from("no handle here")


def test_looks_like_window_rejects_the_minimized_artefact_and_accepts_a_real_one():
    minimised = {"size": (237, 39), "rect": [-32000, -32000, -31763, -31961]}
    normal = {"size": (2582, 1550), "rect": [-11, -11, 2571, 1539]}
    assert capture.looks_like_window(minimised) is False
    assert capture.looks_like_window(normal) is True


def test_image_stats_separate_a_blank_render_from_a_real_one():
    # The sampler steps through the buffer, so the image has to be big enough
    # for the sample count to say anything: 400x300 gives ~300 samples.
    blank = capture.image_stats(make_shot(400, 300, pattern="flat"))
    varied = capture.image_stats(make_shot(400, 300, pattern="gradient"))
    assert blank["distinct_sampled_colors"] == 1
    assert varied["distinct_sampled_colors"] > 50
    # a blank frame is still "not black"; the colour count is what catches it
    assert blank["nonblack_fraction"] > 0.9
    assert blank["sha256"] != varied["sha256"]
    assert blank["sha256"].startswith(blank["sha256_16"])


def test_image_stats_on_an_empty_buffer_reports_zero_rather_than_dividing_by_zero():
    stats = capture.image_stats({"size": (0, 0), "buf": type("B", (), {"raw": b""})()})
    assert stats == {"sampled_pixels": 0, "nonblack_fraction": 0.0,
                     "distinct_sampled_colors": 0, "sha256": "", "sha256_16": ""}


@windows_only
def test_capture_window_settles_and_reports_every_moment(monkeypatch, fake_shot):
    """The settle rule and the minimized guard, on a scripted window."""
    frames = [fake_shot, fake_shot]           # two identical frames -> settled
    states = {"minimized": True}
    restored = {"called": 0, "minimised_again": 0}

    monkeypatch.setattr(capture, "grab_window", lambda hwnd: frames.pop(0))
    monkeypatch.setattr(capture, "is_minimized",
                        lambda hwnd: states["minimized"])
    monkeypatch.setattr(capture, "window_rect", lambda hwnd: [0, 0, 800, 600])
    monkeypatch.setattr(capture, "is_maximized", lambda hwnd: True)
    monkeypatch.setattr(capture, "is_visible", lambda hwnd: True)
    monkeypatch.setattr(capture, "foreground_hwnd", lambda: 0)
    monkeypatch.setattr(capture, "set_foreground", lambda hwnd: True)

    def _show_noactivate(hwnd):
        restored["called"] += 1
        states["minimized"] = False
        return True

    def _show_minimize(hwnd):
        restored["minimised_again"] += 1
        states["minimized"] = True
        return True

    monkeypatch.setattr(capture, "show_noactivate", _show_noactivate)
    monkeypatch.setattr(capture, "show_minimize", _show_minimize)

    shot, info = capture.capture_window(0x1234)
    assert info["minimized_before"] is True
    assert info["auto_restored"] is True
    assert info["focus_restored"] is None          # there was no window to give back
    assert restored["called"] == 1
    assert info["minimized_after"] is True          # put back the way it was found
    assert restored["minimised_again"] == 1
    assert info["settle"]["settled"] is True
    assert info["settle"]["attempts"] == 2
    assert info["looks_like_window"] is True


@windows_only
def test_capture_window_reports_a_frame_that_never_settles(monkeypatch, fake_shot):
    """A frame that keeps changing must be reported as UNSETTLED, not as fresh."""
    counter = {"n": 0}

    def _changing(hwnd):
        counter["n"] += 1
        return make_shot(80, 60, origin=counter["n"])

    monkeypatch.setattr(capture, "grab_window", _changing)
    monkeypatch.setattr(capture, "is_minimized", lambda hwnd: False)
    monkeypatch.setattr(capture, "window_rect", lambda hwnd: [0, 0, 80, 60])
    monkeypatch.setattr(capture, "is_maximized", lambda hwnd: False)
    monkeypatch.setattr(capture, "is_visible", lambda hwnd: True)
    monkeypatch.setattr(capture, "foreground_hwnd", lambda: 0)

    _shot, info = capture.capture_window(0x1234, tries=3, delay=0.0)
    assert info["settle"]["settled"] is False
    assert info["settle"]["attempts"] == 3
    assert len(info["settle"]["history"]) == 3


@windows_only
def test_capture_window_reports_a_minimized_window_it_could_not_recover(monkeypatch,
                                                                       fake_shot):
    """When recovery fails, the frame is refused instead of presented."""
    monkeypatch.setattr(capture, "grab_window",
                        lambda hwnd: {"ok": True, "rect": [-32000, -32000, -31763, -31961],
                                      "size": (237, 39), "buf": fake_shot["buf"],
                                      "rows": 39})
    monkeypatch.setattr(capture, "is_minimized", lambda hwnd: True)
    monkeypatch.setattr(capture, "show_noactivate", lambda hwnd: False)   # recovery fails
    monkeypatch.setattr(capture, "show_minimize", lambda hwnd: True)
    monkeypatch.setattr(capture, "window_rect", lambda hwnd: [-32000, -32000, -31763, -31961])
    monkeypatch.setattr(capture, "is_maximized", lambda hwnd: False)
    monkeypatch.setattr(capture, "is_visible", lambda hwnd: True)
    monkeypatch.setattr(capture, "foreground_hwnd", lambda: 0)

    _shot, info = capture.capture_window(0x1234, settle=False)
    assert info["auto_restored"] is False
    assert info["looks_like_window"] is False


@windows_only
def test_a_real_window_can_be_captured_without_reaper():
    """The Win32 path against a real window handle, not a mock."""
    hwnd = taskbar_hwnd()
    if not hwnd:
        pytest.skip("no shell window is present in this session")
    capture.set_dpi_awareness()
    shot = capture.grab_window(hwnd)
    stats = capture.image_stats(shot)
    assert shot["size"][0] > 0 and shot["size"][1] > 0
    assert len(shot["buf"].raw) == shot["size"][0] * shot["size"][1] * 4
    assert stats["nonblack_fraction"] > 0.5
    assert capture.looks_like_window(shot) is True
    # the screen grab of the same rectangle must exist as a comparison witness
    left, top, right, bottom = shot["rect"]
    control = capture.grab_screen_region(left, top, right - left, bottom - top)
    assert control["size"] == shot["size"]
