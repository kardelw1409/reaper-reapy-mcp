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


def test_a_superseded_artifact_does_not_fail_the_aggregate():
    """MEASURED defect: the apply of a fully verified bundle reported `ok: false`.

    Every one of its eight artifacts said `ok: true`; the aggregate counted the
    bookkeeping entry `_superseded` too, which has no "ok" field at all, and `all()`
    reads an absent value as False. A report entry that is not a measurement must not
    be able to decide the verdict - in either direction.
    """
    expected = manifest.expected_note_spans(
        _info(
            drums_kick={"notes": 568, "last_note_project_s": 250.0},
            vocal={"notes": 289, "last_note_project_s": 217.5},
        )
    )
    actual = {
        "drums_kick": {"notes": 568, "first_s": None, "last_s": 250.0},
        "vocal": {"notes": 289, "first_s": None, "last_s": 217.5},
    }
    warnings, report = manifest.compare_note_spans(expected, actual)
    assert warnings == []
    report["_superseded"] = {
        "artifacts": ["drums"],
        "reason": "the bundle carries per-class drum parts, which are inserted instead",
    }
    assert all(entry["ok"] for key, entry in report.items() if not key.startswith("_"))
    assert manifest.note_report_ok(report) is True


def test_a_real_failure_still_fails_the_aggregate():
    """Excluding bookkeeping must not turn the check into one that always passes."""
    expected = manifest.expected_note_spans(_info(drums={"notes": 2, "last_note_project_s": 100.0}))
    _, report = manifest.compare_note_spans(expected, {"drums": {"notes": 2, "first_s": None, "last_s": 90.0}})
    assert report["drums"]["ok"] is False
    assert manifest.note_report_ok(report) is False

    # and an artifact the project could not measure is a failure, not a pass
    expected = manifest.expected_note_spans(_info(drums={"notes": 10, "first_note_project_s": 1.0}))
    _, unchecked = manifest.compare_note_spans(expected, {})
    assert unchecked["drums"]["checked"] is False
    assert manifest.note_report_ok(unchecked) is False, "an unchecked artifact must not satisfy the aggregate"


def test_a_flat_distribution_is_not_reported_as_a_grid_offset():
    """MEASURED on the real bundle's bass: its deviations are spread over the whole 1/16
    step, so `median deviation` is arbitrary - the SAME file moved by a rigid 8.47 ms
    changed that median by 4.4 ms (-3.58 -> -7.94) while every note had moved by exactly
    the shift. A number that moves 4 ms when nothing moved 4 ms must not be printed as
    "the part sits 7.9 ms off the grid"."""
    step = 0.087202
    # the real histogram: nearly flat over the step (10 ms bins: 73 41 89 119 85 47 43 65)
    flat = [-40.0 + 87.0 * (i / 613.0) for i in range(614)]
    summary = manifest.summarize_note_phase([d / 1000.0 for d in flat], step)
    assert summary["notes"] == 614
    assert summary["phase_is_an_offset"] is False
    assert "NOT how far the part sits off the grid" in summary["note"]
    assert summary["spread_ms"] > 40.0, summary

    # the same notes moved by a rigid 8.5 ms: every note moved, the median barely did
    shifted = manifest.summarize_note_phase([(d + 8.47) / 1000.0 for d in flat], step)
    assert abs(shifted["phase_ms"] - summary["phase_ms"]) < 9.0
    assert shifted["phase_is_an_offset"] is False


def test_a_coherent_set_is_reported_as_a_grid_offset():
    """The other half of the same rule: a part whose notes share a phase (the kick, the
    score parts) gets a number that IS the offset, and the report says which of the two
    cases it is looking at."""
    step = 0.087202
    # deviations (already relative to the grid), all sharing one phase
    coherent = [0.007] * 50
    summary = manifest.summarize_note_phase(coherent, step)
    assert summary["phase_is_an_offset"] is True
    assert abs(summary["phase_ms"] - 7.0) < 1.0, summary
    assert summary["share_on_grid"] == 1.0
    assert summary["spread_ms"] == 0.0
    assert "note" not in summary, "an offset needs no excuse"


def test_the_share_window_decides_the_two_cases():
    """The boundary is a named constant, not a literal, and it is where the real files sat:
    the drum parts and score parts measured 1.00, the bass 0.63."""
    step = 0.087202
    assert manifest.GRID_PHASE_MIN_SHARE == 0.8
    # 79% coherent -> still not an offset; 81% -> an offset (34 ms is outside the 25 ms window)
    loose = [0.004] * 79 + [-0.030] * 21
    tight = [0.004] * 81 + [-0.030] * 19
    assert manifest.summarize_note_phase(loose, step)["phase_is_an_offset"] is False
    assert manifest.summarize_note_phase(tight, step)["phase_is_an_offset"] is True


def test_a_report_with_nothing_to_judge_is_not_a_failure_of_the_check():
    """`all()` over an empty selection is True - which is why the caller ALSO reports
    whether anything was checked (`verification.notes_checked`). The aggregate answers
    "did every measurement match", not "was there a measurement".

    The bookkeeping shape is what the apply actually writes (`_checked`/`_reason`),
    including a bare `reason`: a report must never crash the function that judges it."""
    assert manifest.note_report_ok({}) is True
    assert manifest.note_report_ok({"_checked": False, "_reason": "no midi_time block"}) is True
    assert manifest.note_report_ok({"_checked": False, "reason": "no midi_time block"}) is True
    assert manifest.note_report_ok({"_superseded": {"artifacts": ["drums"]}}) is True
