"""View/scroll/zoom against a scripted host: what is reported, what is refused.

The interesting assertions are about the CHECKS, because each of them can fail
silently:

* the span must be verified tightly while the position cannot be (REAPER
  quantises it) - so the same code path has to report a 20 ms drift as a failure
  when an exact round-trip was requested and as a pass when an arbitrary range
  was written. Both directions are pinned, and the tests are a pair on purpose:
  the relative tolerance is only believable if the tight rule can also fail.
* an action id must never be fired before its NAME was read back. The test makes
  the host answer with a different name and requires a refusal plus the id in the
  answer.
* `dirty_delta` must return None - not 0 - when a counter is unreadable, because
  "0" is the claim that the project was not written.
"""

from __future__ import annotations

import pytest
from vision_support import FakeHost

from reaper_reapy_mcp.vision import view


def test_view_range_reads_the_last_two_of_six_values():
    host = FakeHost(view=(12.5, 44.25))
    assert view.view_range(host) == [12.5, 44.25]


def test_view_range_returns_none_when_the_host_refuses():
    host = FakeHost(fail={"GetSet_ArrangeView2"})
    assert view.view_range(host) is None


def test_set_view_accepts_the_hosts_quantisation_and_reports_it():
    """An arbitrary range: the span is exact, the position is off by 20 ms."""
    host = FakeHost(view=(0.0, 30.0), quantise=-0.020)
    applied = view.set_view(host, 10.0, 40.0, wait=0.0)
    assert applied["ok"] is True
    assert applied["span_error_s"] == pytest.approx(0.0, abs=1e-9)
    assert applied["start_error_s"] == pytest.approx(-0.020, abs=1e-9)
    # 30 s * 5e-4 = 15 ms is LESS than the 50 ms floor, so the floor applies here
    assert applied["tolerance_s"] == pytest.approx(view.DEFAULT_POS_TOL)
    assert applied["error_fraction_of_span"] == pytest.approx(6.7e-4, abs=1e-5)
    assert "tolerance_rule" in applied


def test_the_relative_tolerance_takes_over_on_a_wide_span():
    """A 20 ms error is nothing at 600 s and everything at 30 s, and the rule says
    which one it is looking at."""
    host = FakeHost(view=(0.0, 600.0), quantise=-0.020)
    applied = view.set_view(host, 0.0, 600.0, wait=0.0)
    assert applied["ok"] is True
    assert applied["tolerance_s"] == pytest.approx(600.0 * view.DEFAULT_REL_TOL)
    assert applied["tolerance_s"] > view.DEFAULT_POS_TOL


def test_set_view_fails_when_the_position_is_outside_the_relative_tolerance():
    """The control: the same call must be able to fail, or the pass above is empty."""
    host = FakeHost(view=(0.0, 30.0), quantise=-5.0)
    applied = view.set_view(host, 10.0, 40.0, wait=0.0)
    assert applied["ok"] is False
    assert applied["start_error_s"] == pytest.approx(-5.0, abs=1e-9)


def test_restore_view_is_exact_and_reports_a_drift_instead_of_absorbing_it():
    """A round-trip of a host-produced range was measured exact to 1e-12 s."""
    faithful = FakeHost()
    assert view.restore_view(faithful, [3.0, 33.0], wait=0.0)["ok"] is True

    drifting = FakeHost(quantise=-0.020)
    restored = view.restore_view(drifting, [3.0, 33.0], wait=0.0)
    assert restored["ok"] is False
    assert restored["start_error_s"] == pytest.approx(-0.020, abs=1e-9)
    # this is the pair to the quantisation test: a tolerance that accepts
    # everything here would make the restore check meaningless
    assert restored["tolerance_s"] < 0.001


def test_fire_action_refuses_an_id_whose_name_does_not_match():
    host = FakeHost(names={1012: "View: Something else entirely"})
    fired = view.fire_action(host, "h_zoom_in")
    assert fired["ok"] is False and fired["refused"] is True
    assert fired["expected"] == "View: Zoom in horizontal"
    assert fired["actual"] == "View: Something else entirely"
    assert not [call for call in host.calls if call.startswith("Main_OnCommand")]


def test_fire_action_fires_the_verified_id_the_asked_number_of_times():
    host = FakeHost()
    fired = view.fire_action(host, "v_zoom_in", 3, wait=0.0)
    assert fired["ok"] is True and fired["fired"] == 3
    assert fired["name"] == "View: Zoom in vertical"
    assert host.calls.count("Main_OnCommand:40111") == 3


def test_fire_action_refuses_an_unknown_key_without_touching_the_host():
    host = FakeHost()
    fired = view.fire_action(host, "wobble")
    assert fired["ok"] is False and fired["refused"] is True
    assert host.calls == []


def test_scroll_reports_the_measured_shift_and_pixels_per_unit():
    """1 unit = 16 px was measured on the real host; here the arithmetic is pinned.

    100 units * 16 px / 5 px-per-second = 320 s of view, and the reported
    px-per-unit must come back as 16 from that same measurement.
    """
    host = FakeHost(view=(100.0, 400.0), zoom=5.0)
    result = view.scroll(host, dx=100, wait=0.0)
    assert result["ok"] is True
    assert result["view_shift_s"] == pytest.approx(320.0)
    assert result["px_per_unit"] == pytest.approx(16.0)
    assert result["calls"][0]["axis"] == "dx"


def test_scroll_reports_a_host_error_instead_of_raising():
    """The measured shape of a wrong arity: REAPER raises inside the bridge."""
    host = FakeHost(fail={"CSurf_OnScroll"})
    result = view.scroll(host, dx=50, wait=0.0)
    assert result["ok"] is False
    assert "configured to fail" in result["errors"][0]


def test_dirty_delta_is_none_when_a_counter_cannot_be_read():
    """0 is the claim 'nothing was written'; an unreadable counter must not earn it."""
    assert view.dirty_delta({"state_change_count": 7}, {"state_change_count": 7}) == 0
    assert view.dirty_delta({"state_change_count": None}, {"state_change_count": 7}) is None
    assert view.dirty_delta({"state_change_count": 7}, {"state_change_count": None}) is None


def test_a_range_before_time_zero_is_reported_as_clamped_not_as_a_bad_host():
    """Measured live: zooming out 2x on an 88 s project asked for -58.902 s and
    REAPER answered 0.0 s. That is not quantisation - it is impossible input, and
    the answer must name it."""
    host = FakeHost(view=(0.0, 117.8))

    def clamp(proj, is_set, a, b, start, end):
        # REAPER keeps the SPAN and slides the window (measured live: requested
        # -58.902 s, read back 0.0 s, span exact) - so the double must not shrink
        # the span while clamping, or it tests something else entirely.
        if is_set:
            span = float(end) - float(start)
            start = max(0.0, float(start))
            host.view = [start, start + span]
        return [0, bool(is_set), 0, 0, host.view[0], host.view[1]]

    host.GetSet_ArrangeView2 = clamp
    applied = view.set_view(host, -58.9, 176.7, wait=0.0)
    assert applied["ok"] is False
    assert applied["clamped_at_zero"] is True
    assert applied["span_error_s"] == pytest.approx(0.0, abs=1e-9)
    assert "no time before 0.0 s" in applied["note"]
    assert "SPAN was still honoured" in applied["note"]


def test_a_plain_quantisation_error_is_not_reported_as_clamping():
    """The control: the same field must stay false when the range is valid."""
    host = FakeHost(view=(0.0, 30.0), quantise=-0.020)
    applied = view.set_view(host, 10.0, 40.0, wait=0.0)
    assert applied["clamped_at_zero"] is False
    assert "note" not in applied or "clamped" not in applied.get("note", "")


def test_bar_seconds_uses_the_measured_metre_and_names_the_assumption_otherwise():
    """The live project was 2/4 at 150.121 BPM, so 4/4 cannot be assumed silently."""
    two_four = view.bar_seconds(FakeHost(tempo=150.121, signature=(2, 4, 150.121)))
    assert two_four["source"] == "measurement"
    assert two_four["assumed"] is None
    assert two_four["seconds"] == pytest.approx(2 * 60 / 150.121)
    assert "2/4" in two_four["text"]

    four_four = view.bar_seconds(FakeHost(tempo=120.0, signature=(4, 4, 120.0)))
    assert four_four["seconds"] == pytest.approx(2.0)          # 4 beats at 120 BPM

    unreadable = view.bar_seconds(FakeHost(tempo=120.0, signature=None))
    assert unreadable["source"] == "assumption"
    assert "4/4" in unreadable["assumed"]


def test_bar_seconds_scales_by_the_denominator_not_only_the_numerator():
    """A 6/8 bar is three quarter-note beats, not six - dropping the denominator
    would double the slice length of every scan in that metre."""
    six_eight = view.bar_seconds(FakeHost(tempo=120.0, signature=(6, 8, 120.0)))
    assert six_eight["seconds"] == pytest.approx(6 * 0.5 * 60 / 120.0)


def test_project_meta_carries_the_metre_and_survives_a_missing_one():
    host = FakeHost(tempo=150.121, signature=(2, 4, 150.121))
    meta = view.project_meta(host)
    assert meta["tempo"] == 150.121 and meta["tracks"] == 8
    assert meta["time_signature"]["numerator"] == 2
    assert meta["view"] == [0.0, 30.0]

    without = view.project_meta(FakeHost(signature=None))
    assert without["time_signature"] is None
