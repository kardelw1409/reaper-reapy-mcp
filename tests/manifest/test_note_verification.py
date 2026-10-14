"""Note-level verification: the check that counting notes cannot replace (P0-2).

A MIDI artifact written at a tempo other than the project's is inserted without
complaint and holds the SAME number of notes - only their positions reveal it.
Measured on a real bundle: a 120 bpm drums.mid inside a 172 bpm project put its
last note at 186.7 s of project time instead of 267.7 s, an 80.9 s deficit that
the note-count check reported as a pass.

These tests are offline: they exercise the pure expectation/compare functions,
which is where the judgement lives. The REAPER read-back is a thin adapter over
them (apply._take_note_span).
"""

from __future__ import annotations

from reaper_reapy_mcp.manifest import manifest


def _info(**midi_time) -> dict:
    return {"midi_time": midi_time}


def test_expectations_come_from_the_exporters_measurement():
    info = _info(
        drums={
            "notes": 2500,
            "first_note_project_s": 5.65,
            "last_note_project_s": 267.5,
            "file_bpm": 120.0,
            "bar_offset_source": "marker",
        }
    )
    spans = manifest.expected_note_spans(info)
    assert spans["drums"] == {
        "notes": 2500,
        "first_s": 5.65,
        "last_s": 267.5,
        "file_bpm": 120.0,
        "source": "marker",
    }


def test_a_bundle_without_the_block_declares_nothing_checked():
    assert manifest.expected_note_spans({}) == {}
    assert manifest.expected_note_spans({"midi_time": {"drums": {"file_bpm": 120.0}}}) == {}


def test_matching_span_passes():
    expected = manifest.expected_note_spans(
        _info(drums={"notes": 100, "first_note_project_s": 5.65, "last_note_project_s": 267.5})
    )
    actual = {"drums": {"notes": 100, "first_s": 5.653, "last_s": 267.49}}
    warnings, report = manifest.compare_note_spans(expected, actual)
    assert warnings == []
    assert report["drums"]["ok"] is True


def test_a_tempo_deficit_is_caught_although_the_note_count_matches():
    """The measured defect: same notes, positions 1.43x too early."""
    expected = manifest.expected_note_spans(
        _info(drums={"notes": 2500, "first_note_project_s": 5.65, "last_note_project_s": 267.66})
    )
    actual = {"drums": {"notes": 2500, "first_s": 5.65, "last_s": 186.74}}
    warnings, report = manifest.compare_note_spans(expected, actual)

    assert len(warnings) == 1
    assert "does not match the manifest" in warnings[0]
    assert "last note 186.740 s vs 267.660 s" in warnings[0]
    assert report["drums"]["ok"] is False
    assert report["drums"]["notes"] == 2500, "the note count alone would not have caught this"
    assert report["drums"]["last_delta_s"] == -80.92


def test_a_note_count_mismatch_is_caught_on_its_own():
    expected = manifest.expected_note_spans(_info(bass={"notes": 614, "first_note_project_s": 1.0}))
    actual = {"bass": {"notes": 500, "first_s": 1.0, "last_s": None}}
    warnings, report = manifest.compare_note_spans(expected, actual)

    assert "notes 500 != 614" in warnings[0]
    assert report["bass"]["problems"] == ["notes 500 != 614"]


def test_a_missing_measurement_is_not_a_pass():
    expected = manifest.expected_note_spans(_info(drums={"notes": 10, "first_note_project_s": 1.0}))
    warnings, report = manifest.compare_note_spans(expected, {})

    assert "could not read the notes back - not checked" in warnings[0]
    assert report["drums"]["checked"] is False
    assert "ok" not in report["drums"], "an unchecked artifact must not look verified"


def test_an_artifact_with_no_expected_times_only_checks_the_count():
    expected = manifest.expected_note_spans(_info(ins={"notes": 214}))
    actual = {"ins": {"notes": 214, "first_s": 0.0, "last_s": 278.9}}
    warnings, report = manifest.compare_note_spans(expected, actual)
    assert warnings == []
    assert report["ins"]["ok"] is True


def test_the_tolerance_is_one_tick_scale_not_a_second():
    """20 ms: above a 220 ppq tick at 172 bpm (1.6 ms), below any real tempo error."""
    assert 0.001 < manifest.NOTE_TIME_TOLERANCE_S <= 0.05
    expected = manifest.expected_note_spans(_info(drums={"notes": 1, "last_note_project_s": 100.0}))
    inside, _ = manifest.compare_note_spans(expected, {"drums": {"notes": 1, "first_s": None, "last_s": 100.015}})
    outside, _ = manifest.compare_note_spans(expected, {"drums": {"notes": 1, "first_s": None, "last_s": 100.03}})
    assert inside == []
    assert len(outside) == 1


def test_every_artifact_key_has_a_track():
    """vocal/ins live outside MIDI_TRACK_MAP but must still be verifiable."""
    for key in ("vocal", "ins", "drums", "bass"):
        assert key in manifest.MIDI_ARTIFACT_TRACK, key
