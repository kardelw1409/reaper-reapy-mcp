"""P36: MIDI note bounds are validated BEFORE anything is written (offline).

Mechanism (measured live in P34 and reproduced live in P36): ``add_midi_notes``
accepted a note whose end passed the item end; the host stored it, the
controller's own read path reported ``is_visible: false``, and the note never
sounded (51 visible of 52; P36's live record: 4 stored / 1 visible). The MCP
layer now mirrors the controller's own visibility rule - ``0 <= relative_start <
item_length`` and ``0 < relative_end <= item_length`` (midi_controller.py) - on
the **input** values, per note, before the first write: an invalid note refuses
the WHOLE batch and nothing is written. Nothing is clipped, snapped or moved.

The tests drive the real registered tools through a real ``FastMCP`` instance
and a fake controller (no host, no REAPER): the tool factory is the one the
server uses, so the envelope under test is the envelope clients receive.
``time_to_measure`` is replaced because success responses format measure
strings through the live host; the measure-string *conversion* itself is host
code and is exercised live (run artifacts of P36), not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
for _path in (SRC, REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import mcp_tools  # noqa: E402

ITEM_POSITION = 10.0
ITEM_LENGTH = 4.0


class FakeMidiController:
    """Item [10, 14) with every write recorded; nothing touches a host."""

    def __init__(self, *, position: float = ITEM_POSITION, length: float = ITEM_LENGTH,
                 fail_indices: set[int] | None = None):
        self.position = position
        self.length = length
        self.fail_indices = set(fail_indices or ())
        self.notes: list[tuple[int, object, int, float, float, int]] = []
        self.item_lookups = 0

    def get_item_properties(self, track_index: int, item: object):
        self.item_lookups += 1
        return {"position": self.position, "length": self.length, "name": "", "is_audio": False}

    def add_midi_note(self, track_index: int, item: object, pitch: int, start_time: float,
                      length: float, velocity: int = 96, channel: int = 0) -> bool:
        if len(self.notes) in self.fail_indices:
            return False
        self.notes.append((track_index, item, pitch, start_time, length, velocity))
        return True


@pytest.fixture
def midi_tools(monkeypatch):
    controller = FakeMidiController()
    monkeypatch.setattr(mcp_tools, "time_to_measure", lambda t: f"{t:.6f}")
    monkeypatch.setattr(
        mcp_tools,
        "measure_length_to_time",
        lambda lm, start=0.0: {"0:1,000": 1.0, "0:2,000": 2.0, "1:0,0": 2.0}[str(lm)],
    )
    mcp = FastMCP("p36-midi-bounds")
    mcp_tools.setup_mcp_tools(mcp, controller)
    single = mcp._tool_manager.get_tool("add_midi_note").fn
    batch = mcp._tool_manager.get_tool("add_midi_notes").fn
    assert single is not None and batch is not None
    return controller, single, batch


# ---------------------------------------------------------------------------
# 1-5. the geometry contract, both tools
# ---------------------------------------------------------------------------


def test_a_valid_note_is_written_item_relative(midi_tools):
    controller, single, _ = midi_tools
    payload = single(ctx=None, track_index=1, item=0, pitch=60, start_time=10.5, length_time=0.5)
    assert payload["status"] == "success", payload
    assert controller.notes == [(1, 0, 60, 0.5, 0.5, 96)]


def test_a_note_ending_exactly_at_the_item_end_is_accepted(midi_tools):
    """The host's own rule is `0 < relative_end <= item_length`: equality is inside."""
    controller, single, _ = midi_tools
    payload = single(ctx=None, track_index=1, item=0, pitch=76, start_time=13.5, length_time=0.5)
    assert payload["status"] == "success", payload
    assert controller.notes == [(1, 0, 76, 3.5, 0.5, 96)]


def test_a_note_ending_past_the_item_end_is_refused_with_its_numbers(midi_tools):
    controller, single, _ = midi_tools
    payload = single(ctx=None, track_index=1, item=0, pitch=64, start_time=13.5, length_time=0.500001)
    assert payload["status"] == "error", payload
    assert "past the MIDI item end" in payload["message"], payload
    assert "1e-06" in payload["message"], payload
    assert controller.notes == [], "a refused note must not reach the write path"


def test_a_note_starting_at_or_past_the_item_end_is_refused(midi_tools):
    controller, single, _ = midi_tools
    at_end = single(ctx=None, track_index=1, item=0, pitch=67, start_time=14.0, length_time=0.5)
    beyond = single(ctx=None, track_index=1, item=0, pitch=67, start_time=15.0, length_time=0.5)
    assert at_end["status"] == "error" and beyond["status"] == "error"
    assert controller.notes == []


def test_a_note_before_the_item_is_refused_in_both_modes(midi_tools):
    controller, single, _ = midi_tools
    absolute = single(ctx=None, track_index=1, item=0, pitch=72, start_time=9.5, length_time=0.5)
    relative = single(ctx=None, track_index=1, item=0, pitch=72, start_time=-0.5,
                      length_time=0.5, relative_start=True)
    assert absolute["status"] == "error", absolute
    assert relative["status"] == "error", relative
    assert "before the MIDI item start" in relative["message"], relative
    assert controller.notes == []


# ---------------------------------------------------------------------------
# 6. lengths and invalid fields
# ---------------------------------------------------------------------------


def test_zero_and_negative_lengths_are_refused(midi_tools):
    controller, single, _ = midi_tools
    zero = single(ctx=None, track_index=1, item=0, pitch=60, start_time=10.5, length_time=0.0)
    negative = single(ctx=None, track_index=1, item=0, pitch=60, start_time=10.5, length_time=-1.0)
    zero_measure = single(ctx=None, track_index=1, item=0, pitch=60, start_time=10.5,
                          length_measure="0:0,0")
    assert zero["status"] == "error" and "greater than zero" in zero["message"], zero
    assert negative["status"] == "error" and "greater than zero" in negative["message"], negative
    assert zero_measure["status"] == "error" and "zero length specified" in zero_measure["message"]
    assert controller.notes == []


def test_invalid_pitch_velocity_and_positions_are_refused(midi_tools):
    controller, single, _ = midi_tools
    cases = [
        {"pitch": 200, "start_time": 10.5, "length_time": 0.5},
        {"pitch": 60.5, "start_time": 10.5, "length_time": 0.5},
        {"pitch": True, "start_time": 10.5, "length_time": 0.5},
        {"pitch": 60, "start_time": 10.5, "length_time": 0.5, "velocity": 128},
        {"pitch": 60, "start_time": 10.5, "length_time": 0.5, "velocity": False},
        {"pitch": 60, "start_time": "abc", "length_time": 0.5},
        {"pitch": 60, "start_time": float("nan"), "length_time": 0.5},
        {"pitch": 60, "start_time": 10.5, "length_time": float("inf")},
        {"pitch": 60, "length_time": 0.5},  # no start at all
        {"pitch": 60, "start_time": 10.5},  # no length at all
        {"pitch": 60, "start_time": 10.5, "start_measure": "2:1,000", "length_time": 0.5},
        {"pitch": 60, "start_time": 10.5, "length_time": 0.5, "length_measure": "0:1,000"},
    ]
    for note in cases:
        payload = single(ctx=None, track_index=1, item=0, **note)
        assert payload["status"] == "error", (note, payload)
    assert controller.notes == []


def test_an_existing_item_is_required(midi_tools):
    controller, single, batch = midi_tools
    controller.get_item_properties = lambda track_index, item: {}
    payload = single(ctx=None, track_index=1, item=0, pitch=60, start_time=10.5, length_time=0.5)
    assert payload["status"] == "error" and "does not exist" in payload["message"]
    multi = batch(ctx=None, track_index=1, item=0, notes=[{"pitch": 60, "start_time": 10.5,
                                                           "length_time": 0.5}])
    assert multi["status"] == "error" and multi["failed_notes"], multi
    assert controller.notes == []


# ---------------------------------------------------------------------------
# 7-9. the batch: validate the whole list before the first write
# ---------------------------------------------------------------------------


def test_a_mixed_batch_is_refused_whole_and_writes_nothing(midi_tools):
    controller, _, batch = midi_tools
    notes = [
        {"pitch": 36, "start_time": 10.0, "length_time": 0.5},
        {"pitch": 38, "start_time": 11.0, "length_time": 0.5},
        {"pitch": 42, "start_time": 12.0, "length_time": 0.5},
        {"pitch": 46, "start_time": 13.7, "length_time": 0.6},  # ends 0.3 s past
    ]
    payload = batch(ctx=None, track_index=1, item=0, notes=notes)
    assert payload["status"] == "error", payload
    assert payload["successful_notes"] == [], "no partial MIDI content may be left behind"
    assert [row["index"] for row in payload["failed_notes"]] == [3]
    assert "whole batch was refused" in payload["message"]
    assert controller.notes == [], "validation must finish before the first write"
    assert controller.item_lookups == 1, "one item read serves the whole batch"


def test_a_valid_batch_writes_every_note_in_order(midi_tools):
    controller, _, batch = midi_tools
    notes = [
        {"pitch": 36, "start_time": 10.0, "length_time": 0.5, "velocity": 100},
        {"pitch": 38, "start_time": 11.0, "length_time": 0.5},
        {"pitch": 42, "start_time": 12.0, "length_time": 0.5},
        {"pitch": 42, "start_time": 13.5, "length_time": 0.5},  # ends exactly at the item end
    ]
    payload = batch(ctx=None, track_index=1, item="MediaItem*0x1", notes=notes)
    assert payload["status"] == "success", payload
    assert len(payload["successful_notes"]) == 4
    assert controller.notes == [
        (1, "MediaItem*0x1", 36, 0.0, 0.5, 100),
        (1, "MediaItem*0x1", 38, 1.0, 0.5, 96),
        (1, "MediaItem*0x1", 42, 2.0, 0.5, 96),
        (1, "MediaItem*0x1", 42, 3.5, 0.5, 96),
    ]


def test_a_host_side_write_failure_mid_batch_is_reported_as_partial(midi_tools, monkeypatch):
    """Validation can no longer refuse a note, so 'partial' means the HOST failed."""
    controller = FakeMidiController(fail_indices={1})
    monkeypatch.setattr(mcp_tools, "time_to_measure", lambda t: f"{t:.6f}")
    mcp = FastMCP("p36-midi-partial")
    mcp_tools.setup_mcp_tools(mcp, controller)
    batch = mcp._tool_manager.get_tool("add_midi_notes").fn
    notes = [
        {"pitch": 36, "start_time": 10.0, "length_time": 0.5},
        {"pitch": 38, "start_time": 11.0, "length_time": 0.5},
    ]
    payload = batch(ctx=None, track_index=1, item=0, notes=notes)
    assert payload["status"] == "partial", payload
    assert len(payload["successful_notes"]) == 1 and len(payload["failed_notes"]) == 1
    assert payload["failed_notes"][0]["index"] == 1
    assert [tuple(row[:3]) for row in controller.notes] == [(1, 0, 36)], (
        "exactly the notes the host accepted are recorded"
    )


# ---------------------------------------------------------------------------
# 10. backward compatibility of the coordinate contract
# ---------------------------------------------------------------------------


def test_relative_mode_and_per_note_overrides_keep_their_meaning(midi_tools):
    controller, _, batch = midi_tools
    notes = [
        {"pitch": 60, "start_time": 0.5, "length_time": 0.5},
        {"pitch": 64, "start_time": 11.0, "length_time": 0.5, "relative_start": False},
        {"pitch": 67, "start_measure": "0:1,000", "length_measure": "0:1,000", "relative_start": True},
    ]
    payload = batch(ctx=None, track_index=1, item=0, notes=notes, relative_start=True)
    assert payload["status"] == "success", payload
    assert controller.notes == [
        (1, 0, 60, 0.5, 0.5, 96),   # relative: exactly the offset
        (1, 0, 64, 1.0, 0.5, 96),   # per-note absolute override: 11.0 - 10.0
        (1, 0, 67, 1.0, 1.0, 96),   # relative measure start 0:1,000 -> 1.0 s in
    ]
    assert payload["successful_notes"][0]["details"]["relative_start_mode"] is True


def test_an_empty_note_list_is_refused(midi_tools):
    _, _, batch = midi_tools
    payload = batch(ctx=None, track_index=1, item=0, notes=[])
    assert payload["status"] == "error" and "non-empty list" in payload["message"], payload
