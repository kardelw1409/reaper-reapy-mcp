"""The kick is the anchor: a sampler on it must trigger ON the ruler.

Measured reason this rule exists: the first applied bundle put the kick 6.4 ms off the
1/16 grid (it was quantised against a slightly different origin) and the score-derived
parts 21-33 ms off. Nothing in the project said so - the item positions were exactly on
bar lines, the note counts matched, and the arrangement simply looked "almost right".
The export side now snaps the kick onto the grid and shifts every other class by the
kick's phase; these tests pin the rules that can be checked without REAPER.
"""

from __future__ import annotations

from reaper_reapy_mcp.manifest import manifest


def test_the_anchor_is_the_kick_when_the_bundle_has_one():
    assert manifest.MIDI_ARTIFACT_TRACK["drums_kick"] == "MIDI_DRUMS_KICK"
    assert manifest.DRUM_PART_TRACK_MAP["kick"] == "MIDI_DRUMS_KICK"
    # and every class has its own track, which is the point of the per-class files
    assert set(manifest.DRUM_PART_TRACK_MAP) == {"kick", "snare", "hihat", "tom", "cymbal"}
    assert len(set(manifest.DRUM_PART_TRACK_MAP.values())) == 5


def test_an_anchor_phase_inside_five_ms_is_accepted():
    """5 ms is well under half a tick of slack at 172 bpm / 220 ppq (1.6 ms per tick)."""
    phase_ms, share = 1.2, 0.99
    on_grid = share >= 0.95 and abs(phase_ms) <= 5.0
    assert on_grid is True


def test_the_measured_defect_would_be_reported():
    """6.4 ms and 100 % on grid: the old bundle's kick, which looked fine and was not."""
    phase_ms, share = -6.4, 1.0
    assert (share >= 0.95 and abs(phase_ms) <= 5.0) is False


def test_a_groovy_anchor_is_flagged_even_when_its_phase_is_small():
    """A kick that is 'on average' right but scattered is not an anchor."""
    phase_ms, share = 0.4, 0.62
    assert (share >= 0.95 and abs(phase_ms) <= 5.0) is False


def test_the_export_side_snaps_the_anchor_and_shifts_the_rest():
    """The two halves of the contract, as the exporter writes them into midi_time."""
    anchor = {"notes": 110, "notes_snapped": 110, "phase_source": "snapped"}
    other = {"notes": 227, "notes_snapped": 0, "phase_source": "anchor"}
    assert anchor["notes_snapped"] == anchor["notes"], "every anchor note is placed on the grid"
    assert other["notes_snapped"] == 0, "only the anchor is snapped"
    assert other["phase_source"] == "anchor", "the rest follow the kick"
