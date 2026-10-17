"""Cut by region: the MIDI is inserted as per-region items, not as one continuous block.

The user's own prescription (2026-10-18): "insert the MIDI in pieces under the regions - not as one
continuous signal", said after a house take came out with each part lying across the whole
arrangement. The rows come from the normalizer, which measured them in the AUDIO (one per region that
holds NOTES, spanning the region's own bar-line boundaries); the exporter re-records them in project
seconds and the fill turns each row into an item. These tests are offline: the slicing is a pure
function and the manifest reading is pure Python.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_reapy_mcp.manifest import apply, manifest


def _action(**over) -> dict:
    base = {"track": "MIDI_BASS", "kind": "midi", "file": "bass.mid", "start_s": 31.475, "artifact": "bass"}
    return {**base, **over}


def test_a_part_with_segments_becomes_one_action_per_region_at_the_region_boundaries():
    """The item's START is origin + the row's start, its LENGTH is the row's span, and its source
    offset is the row's start - so the notes keep sounding where the regions say they are."""
    segments = {
        "bass": [
            {"start_s": 1.967, "end_s": 31.475, "notes": 46, "name": "intro"},
            {"start_s": 63.434, "end_s": 94.426, "notes": 146, "name": "chorus"},
        ]
    }
    sliced = apply.cut_actions_by_segments([_action()], segments, origin_s=31.475)

    assert len(sliced) == 2
    first, second = sliced
    assert first["start_s"] == pytest.approx(31.475 + 1.967, abs=1e-6)
    assert first["length_s"] == pytest.approx(31.475 - 1.967, abs=1e-6)
    assert first["source_offset_s"] == pytest.approx(1.967, abs=1e-6)
    assert (first["segment"], first["segment_name"], first["segment_notes"]) == (0, "intro", 46)
    # THE GAP IS THE POINT: nothing is inserted between the regions, so the breakdown is silent
    assert second["start_s"] == pytest.approx(31.475 + 63.434, abs=1e-6)
    assert second["start_s"] > first["start_s"] + first["length_s"] + 30.0


def test_a_part_without_segments_keeps_its_single_item():
    """No measured runs (an older bundle, or a class the normalizer did not cut) must behave exactly
    as before - one item from the arrangement origin."""
    original = [_action(), {"track": "AI_MASTER", "kind": "audio", "file": "song.wav", "start_s": 31.475}]
    sliced = apply.cut_actions_by_segments(original, {"drums_kick": [{"start_s": 0.0, "end_s": 10.0}]}, origin_s=31.475)
    assert sliced == original


def test_an_audio_action_is_never_sliced_even_if_its_key_has_segments():
    audio = {"track": "AI_MASTER", "kind": "audio", "file": "song.wav", "start_s": 31.475, "artifact": "bass"}
    sliced = apply.cut_actions_by_segments([audio], {"bass": [{"start_s": 1.0, "end_s": 2.0, "notes": 3}]}, origin_s=31.475)
    assert sliced == [audio]


def test_a_take_holds_the_whole_file_so_only_the_notes_inside_the_item_sound():
    """MEASURED defect (afro take, 2026-10-18): a take's note list is the SOURCE's, whatever the
    item's source offset is - so a part cut into three slices read 288 notes against the file's 96
    (exactly 3x) and the fill refused the project as unverified. The notes that SOUND are the ones
    inside the item, and the union over a track's items must count each note exactly once."""
    # one source file whose notes sit at project 10, 20 and 30 (as REAPER maps them per item)
    source_times = [10.0, 20.0, 30.0]
    first = apply.notes_in_item_window(source_times, 10.0, 10.0)  # item [10, 20]
    second = apply.notes_in_item_window(source_times, 20.0, 10.0)  # item [20, 30]
    assert first == [10.0, 20.0], first
    assert second == [20.0, 30.0], second
    # the boundary note is in BOTH windows (it is audible at the edge); the count follows the windows
    assert sum(len(part) for part in (apply.notes_in_item_window(source_times, 10.0, 10.0),
                                      apply.notes_in_item_window(source_times, 20.0, 10.0))) == 4
    # an UNSLICED item covers its whole source: nothing is filtered away
    assert apply.notes_in_item_window(source_times, 10.0, 100.0) == source_times


def test_the_expectation_of_a_cut_part_is_the_union_of_its_runs():
    """MEASURED (afro take): without this the tom part was reported as "notes 118 != 119; last note
    139.181 s vs 140.903 s" although every note that SOUNDS sat exactly where the manifest says - the
    missing one lies past the last region and is deliberately not inserted, because nothing plays
    there."""
    info = {
        "midi_time": {"drums_tom": {"notes": 119, "first_note_project_s": 31.5, "last_note_project_s": 140.903}},
        "midi_segments": {
            "drums_tom": [
                {"start_s": 31.5, "end_s": 51.2, "notes": 10, "name": "intro",
                 "first_note_s": 33.4, "last_note_s": 50.1},
                {"start_s": 51.2, "end_s": 74.8, "notes": 25, "name": "verse",
                 "first_note_s": 52.0, "last_note_s": 74.5},
                {"start_s": 74.8, "end_s": 139.7, "notes": 83, "name": "chorus",
                 "first_note_s": 78.9, "last_note_s": 139.181},
            ]
        },
    }
    spans = manifest.expected_note_spans(info)
    tom = spans["drums_tom"]
    assert tom["notes"] == 118, tom  # the counts ADD UP: 10 + 25 + 83
    assert tom["first_s"] == 33.4
    assert tom["last_s"] == 139.181
    assert tom["sliced_into"] == 3
    assert [row["name"] for row in tom["slices"]] == ["intro", "verse", "chorus"]

    # ...and an artifact with NO runs keeps the whole-file expectation
    info["midi_time"]["bass"] = {"notes": 88, "first_note_project_s": 78.7, "last_note_project_s": 133.3}
    assert manifest.expected_note_spans(info)["bass"]["notes"] == 88


def _bundle(tmp_path: Path, *, midi_segments) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("song.wav", "score.abc", "vocal.mid", "ins.mid", "drums.mid", "bass.mid"):
        (bundle / name).write_bytes(b"x")
    document = {
        "schema": manifest.SCHEMA,
        "version": manifest.VERSION,
        "title": "cut-test",
        "bpm": 122.0,
        "meter": [4, 4],
        "duration_s": 160.0,
        "audio": {"file": "song.wav", "start_s": 31.475, "source_offset_s": 0.487},
        "midi": {"vocal": "vocal.mid", "ins": "ins.mid", "drums": "drums.mid", "bass": "bass.mid"},
        "regions": [{"name": "intro", "start_s": 31.475, "end_s": 62.95, "source": "audio"}],
        "grid": {"first_downbeat_s": 0.487, "bar_s": 1.967, "bpm_project": 122.0},
    }
    if midi_segments is not None:
        document["midi_segments"] = midi_segments
    (bundle / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    return bundle


def test_the_manifest_reads_the_segments_and_keeps_only_usable_rows(tmp_path):
    bundle = _bundle(
        tmp_path,
        midi_segments={
            "bass": {
                "file": "bass.mid",
                "segments": [
                    {"start_s": 1.967, "end_s": 31.475, "notes": 46, "name": "intro"},
                    {"start_s": 31.475, "end_s": 31.475, "notes": 0},  # zero length: dropped
                    {"start_s": "nonsense", "end_s": 5.0},  # unparsable: dropped
                    {"start_s": 63.434, "end_s": 94.426, "notes": 146, "name": "chorus"},
                ],
            }
        },
    )
    info = manifest.load_manifest(bundle)
    rows = info["midi_segments"]["bass"]
    assert [row["name"] for row in rows] == ["intro", "chorus"]
    assert rows[0]["notes"] == 46


def test_a_bundle_without_the_block_reads_as_no_segments(tmp_path):
    info = manifest.load_manifest(_bundle(tmp_path, midi_segments=None))
    assert info["midi_segments"] == {}
