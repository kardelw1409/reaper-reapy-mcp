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
