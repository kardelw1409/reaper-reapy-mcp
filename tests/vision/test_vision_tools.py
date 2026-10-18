"""The `vision_*` tools as the model meets them: schema, envelope, image content.

Three failure classes are pinned here, each of which has happened in this
workspace:

* a parameter the body reads but the SIGNATURE does not declare is dropped by the
  server without a word, so the call looks successful and does something else.
  Every tool's schema is therefore asserted against the list of arguments its body
  actually uses.
* a registered tool that cannot reach its method still lists, and fails on the
  first live call - so the bodies are CALLED here, against a fake host, and must
  answer with a typed error rather than raising.
* a tool that claims to have sent no image while sending one (and the reverse) is
  invisible from the JSON alone - so the returned content blocks are inspected.

The composition path is real: a synthetic frame goes through the actual encoder
and the actual contact-sheet builder, into a real MCP `ImageContent`.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from vision_support import FakeHost, make_shot

from mcp.server.fastmcp import FastMCP

from reaper_reapy_mcp.vision import capture, imaging, tools

# host and server come from conftest.py: they are shared with the legend and
# settings test modules, and a second copy here would be a copy that drifts.


def call(mcp, name: str, arguments: dict | None = None):
    """Call a tool the way a client does and split the answer into blocks."""
    content = asyncio.run(mcp.call_tool(name, arguments or {}))
    if isinstance(content, tuple):          # (unstructured, structured)
        content = content[0]
    images = [block for block in content if getattr(block, "type", None) == "image"]
    texts = [block for block in content if getattr(block, "type", None) == "text"]
    payload = json.loads(texts[-1].text) if texts else None
    return images, payload


def test_every_tool_is_registered_and_declares_the_arguments_its_body_uses(server):
    mcp, _frames = server
    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert registered == set(tools.TOOL_NAMES)
    declared = {
        "vision_state": set(),
        "vision_shot": {"label", "max_width", "crop", "include_image", "activate",
                        "activate_timeout", "restore_focus"},
        "vision_view": {"start", "end", "shift", "restore", "shot", "label",
                        "max_width", "pos_tol"},
        "vision_scroll": {"dx", "dy", "measure", "shot", "restore", "label",
                          "max_width"},
        "vision_zoom": {"h", "h_steps", "v", "v_steps", "center", "restore", "shot",
                        "label", "max_width", "pos_tol"},
        "vision_scan": {"span_bars", "span_seconds", "overlap", "start", "end",
                        "max_tiles", "columns", "contact_sheet", "include_tiles",
                        "tile_max_width", "label", "pos_tol", "restore", "crop",
                        "activate", "activate_timeout", "restore_focus"},
    }
    for tool in asyncio.run(mcp.list_tools()):
        schema = tool.inputSchema or {}
        assert set(schema.get("properties", {})) == declared[tool.name], tool.name
        assert schema.get("required", []) == [], (
            f"{tool.name} must be callable with no arguments; a required argument "
            "turns a reasonable question into a transport error")
        assert tool.outputSchema is None, (
            f"{tool.name} returns content blocks (image + JSON), not structured "
            "output")


def test_state_answers_with_the_host_state_and_no_image(server, host):
    mcp, _frames = server
    images, payload = call(mcp, "vision_state")
    assert images == []
    assert payload["status"] == "success"
    assert payload["window"]["hwnd"] == "0x1234"
    assert payload["project"]["tempo"] == pytest.approx(150.121)
    assert payload["project"]["time_signature"]["denominator"] == 4
    assert payload["dirty"]["state_change_count"] == 10
    assert payload["imaging"]["available"] is True


def test_shot_sends_the_image_and_writes_the_file_with_its_sidecar(server, tmp_path):
    mcp, _frames = server
    images, payload = call(mcp, "vision_shot", {"label": "probe"})
    assert len(images) == 1
    sent = base64.b64decode(images[0].data)
    assert images[0].mimeType == "image/png"
    assert sent[:8] == b"\x89PNG\r\n\x1a\n"
    assert payload["status"] == "success"
    assert payload["png"].endswith(".png")
    assert payload["png_bytes"] == len(sent), "the file must be what was sent"
    # the sidecar is what makes an image self-describing later
    sidecar = json.loads(open(payload["meta_json"], encoding="utf-8").read())
    assert sidecar["view"] == [0.0, 30.0]
    assert sidecar["frame_size"] == [640, 400]
    assert payload["state_change_delta"] == 0


def test_shot_with_include_image_false_writes_the_file_and_sends_no_image(server):
    mcp, _frames = server
    images, payload = call(mcp, "vision_shot", {"include_image": False, "label": "quiet"})
    assert images == []
    assert payload["status"] == "success"
    assert payload["png"].endswith(".png")
    assert "no image was sent" in payload["note"]


def test_shot_downscales_to_max_width(server):
    mcp, _frames = server
    images, payload = call(mcp, "vision_shot", {"max_width": 320})
    assert payload["image_bytes"] == len(base64.b64decode(images[0].data))
    assert payload["image_max_width"] == 320
    # the frame was 640 wide, so the sent image must be the resized one
    sent_pixels = imaging._PILImage.open(__import__("io").BytesIO(
        base64.b64decode(images[0].data)))
    assert sent_pixels.width == 320


def test_shot_refuses_a_broken_pattern_and_a_bad_crop(server, monkeypatch):
    mcp, _frames = server
    refuse = {"minimized_before": True, "auto_restored": False, "focus_restored": None,
              "minimized_after": True, "maximized": False, "visible": True,
              "foreground_before": "0x1", "rect": [-32000, -32000, -31763, -31961],
              "settle": {"settled": True, "attempts": 1, "history": []},
              "capture_ms_total": 2.0, "size": [237, 39],
              "stats": {"nonblack_fraction": 1.0, "distinct_sampled_colors": 4,
                        "sha256": "x", "sha256_16": "x"},
              "looks_like_window": False}
    monkeypatch.setattr(capture, "capture_window", lambda hwnd, **kw: (make_shot(237, 39), refuse))
    images, payload = call(mcp, "vision_shot", {})
    assert images == [], "an unusable frame must not be sent as a picture"
    assert payload["status"] == "error"
    assert payload["looks_like_window"] is False

    monkeypatch.undo()
    images, payload = call(mcp, "vision_shot", {"crop": "not-a-crop"})
    assert payload["status"] == "error"
    assert "crop" in payload["message"]


def test_view_needs_a_target_and_says_what_to_give(server):
    mcp, _frames = server
    images, payload = call(mcp, "vision_view", {})
    assert payload["status"] == "error"
    assert any("vision_view" in hint for hint in payload["hints"])


def test_view_shifts_the_range_and_restores_it_exactly(server, host):
    mcp, _frames = server
    images, payload = call(mcp, "vision_view", {"shift": 15.0})
    assert payload["status"] == "success"
    assert payload["set"]["requested"] == [15.0, 45.0]
    assert payload["restore"]["ok"] is True
    assert payload["restore"]["read_back"] == [0.0, 30.0]
    assert payload["view_after"] == [0.0, 30.0]
    assert payload["state_change_delta"] == 0
    assert images == []


def test_view_with_shot_returns_the_frame_of_the_new_range(server, host):
    mcp, _frames = server
    images, payload = call(mcp, "vision_view", {"start": 0.0, "end": 12.0, "shot": True})
    assert len(images) == 1
    assert payload["capture"]["view"] == [0.0, 12.0]
    assert payload["view_after"] == [0.0, 30.0]


def test_scroll_without_a_direction_is_refused_with_the_ways_to_ask(server):
    mcp, _frames = server
    _images, payload = call(mcp, "vision_scroll", {})
    assert payload["status"] == "error"
    assert any("vision_scroll" in hint for hint in payload["hints"])


def test_scroll_reports_the_measured_shift_and_the_restore(server, host):
    mcp, _frames = server
    _images, payload = call(mcp, "vision_scroll", {"dx": 100, "measure": True})
    assert payload["status"] == "success"
    assert payload["scroll"]["px_per_unit"] == pytest.approx(16.0)
    assert payload["restore"]["ok"] is True
    assert payload["view_after"] == [0.0, 30.0]


def test_zoom_refuses_nonsense_and_applies_an_exact_factor(server, host):
    mcp, _frames = server
    _images, payload = call(mcp, "vision_zoom", {})
    assert payload["status"] == "error"

    _images, payload = call(mcp, "vision_zoom", {"h": "sideways"})
    assert payload["status"] == "error"

    _images, payload = call(mcp, "vision_zoom", {"h": -1})
    assert payload["status"] == "error"

    _images, payload = call(mcp, "vision_zoom", {"h": 0.5, "restore": False})
    assert payload["status"] == "success"
    span = payload["horizontal"]["requested"]
    assert span[1] - span[0] == pytest.approx(15.0)     # half of the 30 s view
    assert "restore=false" in payload["note"]


def test_scan_uses_the_measured_metre_for_bars(server, host, tmp_path):
    mcp, _frames = server
    _images, payload = call(mcp, "vision_scan", {"span_bars": 4, "max_tiles": 3,
                                                 "contact_sheet": False})
    assert payload["status"] == "success"
    assert payload["bar_source"] == "measurement"
    assert payload["bar_assumed"] is None
    assert payload["time_signature"] == {"numerator": 2, "denominator": 4,
                                        "bpm": 150.121,
                                        "source": "TimeMap_GetTimeSigAtTime"}
    # 4 bars of 2/4 at 150.121 BPM; the answer rounds to a tenth of a millisecond
    assert payload["slice_seconds"] == pytest.approx(4 * 2 * 60 / 150.121, abs=5e-5)
    assert payload["tile_count"] == 3
    for tile in payload["tiles"]:
        assert tile["view_set_ok"] is True
        assert tile["png"].endswith(".png")
    assert payload["restore"]["ok"] is True
    assert payload["state_change_delta"] == 0


def test_scan_builds_one_contact_sheet_from_downscaled_tiles(server, host):
    mcp, _frames = server
    images, payload = call(mcp, "vision_scan", {"span_seconds": 4.0, "max_tiles": 4})
    assert len(images) == 1, "a sheet is one image, not one per tile"
    sheet = base64.b64decode(images[0].data)
    opened = imaging._PILImage.open(__import__("io").BytesIO(sheet))
    # four tiles at 640 wide, two per row -> a sheet wider than one tile
    assert opened.width >= 2 * 640
    assert payload["sheet_meta"].endswith(".json")
    assert payload["sheet_bytes"] == len(sheet)


def test_scan_that_max_tiles_cut_short_says_which_seconds_it_missed(server, host):
    """Measured live: six 15 s tiles of an 88 s project stopped at 86.25 s and the
    answer did not mention the gap, so a caller could believe it had seen the end."""
    mcp, _frames = server
    _images, payload = call(mcp, "vision_scan", {
        "span_seconds": 20.0, "max_tiles": 2, "contact_sheet": False})
    assert payload["status"] == "success"
    assert payload["tile_count"] == 2
    assert payload["truncated"] is True
    # step is span * (1 - overlap): 20 s tiles advance 19 s, so two of them reach 39 s
    assert payload["step_seconds"] == pytest.approx(19.0)
    assert payload["covered"] == [0.0, pytest.approx(39.0)]
    assert payload["region"][1] == pytest.approx(87.928836)
    assert "was NOT covered" in payload["truncated_note"]
    assert "86.250" not in payload["truncated_note"]      # that was the live case
    assert "39.000-87.929" in payload["truncated_note"]


def test_a_scan_that_reached_the_end_is_not_reported_as_truncated(server, host):
    """The control: the same field must stay false when nothing was missed."""
    mcp, _frames = server
    _images, payload = call(mcp, "vision_scan", {
        "span_seconds": 40.0, "max_tiles": 10, "contact_sheet": False})
    assert payload["tile_count"] >= 3
    assert payload["truncated"] is False
    assert payload["truncated_note"] is None
    # `covered` is REPORTED rounded to 0.1 ms, so it is compared at that precision;
    # the truncation decision itself uses the unrounded value.
    assert payload["covered"][1] == pytest.approx(payload["region"][1], abs=1e-4)


def test_the_live_case_of_a_last_tile_clamped_to_the_project_end(server, host):
    """Reproduces the run that exposed the rounding fault, to the numbers.

    Live: six 16 s tiles at 150.121 BPM on an 87.928836 s project. The last tile is
    clamped to the project end, so nothing is uncovered - and the answer must say
    so instead of printing the same second twice.
    """
    mcp, _frames = server
    _images, payload = call(mcp, "vision_scan", {
        "span_seconds": 16.0, "max_tiles": 6, "contact_sheet": False})
    assert payload["tile_count"] == 6
    assert payload["tiles"][-1]["end_s"] == pytest.approx(87.9288, abs=1e-4)
    assert payload["truncated"] is False, (
        "the last tile was clamped to the project end, so no seconds were missed")
    assert payload["truncated_note"] is None
    assert payload["covered"][1] == pytest.approx(payload["region"][1], abs=1e-4)


def test_scan_crop_shrinks_the_tiles_to_the_requested_region(server, host):
    """The window is 640x400 in the fixture; cropping to the right half must make
    the sheet narrower and the tiles free of the static panel."""
    mcp, _frames = server
    plain, plain_payload = call(mcp, "vision_scan", {
        "span_seconds": 4.0, "max_tiles": 2, "tile_max_width": 640})
    cropped, cropped_payload = call(mcp, "vision_scan", {
        "span_seconds": 4.0, "max_tiles": 2, "tile_max_width": 640,
        "crop": "320,0,320,400"})
    plain_size = imaging._PILImage.open(__import__("io").BytesIO(
        base64.b64decode(plain[0].data)))
    cropped_size = imaging._PILImage.open(__import__("io").BytesIO(
        base64.b64decode(cropped[0].data)))
    # Two columns: the uncropped sheet is 2 x 640 wide, the cropped one 2 x 320.
    # The HEIGHT is the same on purpose - cropping the right half leaves a tile
    # that is narrower, not shorter, and 320 px is under tile_max_width so nothing
    # is downscaled.
    assert plain_size.width == 1280
    assert cropped_size.width == 640
    assert cropped_size.height == plain_size.height
    assert cropped_payload["crop"] == "320,0,320,400"
    assert plain_payload["crop"] is None
    # crop coordinates are WINDOW pixels, so the scan must name the frame they
    # are relative to (the window geometry changes between sessions). The rect
    # comes from the captured frame itself: make_shot puts it at (10, 20).
    assert cropped_payload["geometry"]["frame_size"] == [640, 400]
    assert cropped_payload["geometry"]["crop"] == "320,0,320,400"
    assert cropped_payload["geometry"]["window_rect"] == [10, 20, 650, 420]
    assert "top-left" in cropped_payload["geometry"]["crop_is_relative_to"]
    # the full-resolution file on disk must carry the same crop
    on_disk = imaging._PILImage.open(cropped_payload["tiles"][0]["png"])
    assert on_disk.size == (320, 400)
    # and both sheets keep the whole frame's height
    assert on_disk.height == 400


def test_scan_sends_each_tile_when_asked_and_caps_the_payload(server, host):
    mcp, _frames = server
    images, payload = call(mcp, "vision_scan", {"span_seconds": 3.0, "max_tiles": 10,
                                                "include_tiles": True})
    assert 1 <= len(images) <= 8
    assert payload["tiles_sent"] == len(images)


def test_a_host_that_cannot_be_reached_is_a_typed_error_not_an_exception(server, monkeypatch):
    mcp, _frames = server

    def _broken():
        raise tools.view.HostUnavailable("reapy is not available in this process")

    monkeypatch.setattr(tools, "_host", _broken)
    for name in tools.TOOL_NAMES:
        images, payload = call(mcp, name, {})
        assert images == []
        assert payload["status"] == "error", name
        assert "reapy is not available" in payload["message"]
        assert payload["hints"]


def test_an_unexpected_failure_inside_a_tool_still_answers(server, monkeypatch):
    mcp, _frames = server
    monkeypatch.setattr(capture, "capture_window",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    _images, payload = call(mcp, "vision_shot", {})
    assert payload["status"] == "error"
    assert "boom" in payload["message"]
