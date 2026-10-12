"""P26 analysis: conversion shapes, ranges, discrete states, classification.

Brief §17 scenario map for this file (1-11):

1. a continuous linear control with declared bounds;
2. a non-linear control (power / exponential / logarithmic);
3. a control that prints units;
4. an enumeration with measured transitions;
5. a boolean/toggle;
6. an integer-like control with quantisation;
7. a ramp-like control (a numeric display that must not become an enum);
8. a continuous control whose display rounds its text;
9. a control whose range is unknown;
10. sources that contradict each other;
11. an enumeration with empty (unnamed) states.

Every assertion is one of: the *chosen* model, the error on points the fit never
saw, the reason recorded when nothing was chosen, or the state that says a result
is incomplete. "It looked about right" is not testable here, on purpose.
"""

from __future__ import annotations

import math

import pytest

from reaper_reapy_mcp.plugins.discovery.classify import classify_control
from reaper_reapy_mcp.plugins.discovery.discrete import analyze_discrete, sampled_ramp_evidence
from reaper_reapy_mcp.plugins.discovery.mapping import analyze_range, display_bounds, fit_mapping
from reaper_reapy_mcp.plugins.discovery.observe import ObservationStore
from reaper_reapy_mcp.plugins.discovery.text import printed_resolution
from reaper_reapy_mcp.plugins.discovery.types import StepSizes


def grid(points: int = 17) -> list[float]:
    step = 1.0 / (points - 1)
    return [index * step for index in range(points)]


def texts_for(positions: list[float], fn) -> list[str]:
    return [fn(value) for value in positions]


def analyze(positions: list[float], texts: list[str]) -> dict:
    """Run the mapping, discrete and classification stages once, as the pipeline does."""
    from reaper_reapy_mcp.plugins.discovery.mapping import numeric_samples

    points = numeric_samples(positions, texts)
    resolution = printed_resolution([point.y for point in points], texts).to_dict()
    mapping = fit_mapping(positions, texts, resolution=resolution)
    discrete = analyze_discrete(
        index=0, positions=positions, texts=texts, mapping=mapping, resolution=resolution
    )
    kind = classify_control(
        positions=positions, texts=texts, discrete=discrete, mapping=mapping, resolution=resolution
    )
    return {"mapping": mapping, "discrete": discrete, "class": kind, "resolution": resolution}


# ---------------------------------------------------------------------------
# 1-3, 9: numeric controls
# ---------------------------------------------------------------------------


def test_scenario_1_linear_control_is_modelled_affine_with_the_display_range():
    positions = grid()
    texts = texts_for(positions, lambda t: f"{20.0 + 280.0 * t:.1f} semitones")
    result = analyze(positions, texts)
    assert result["mapping"].chosen.kind == "affine"
    assert result["mapping"].chosen.n_test > 0
    assert result["mapping"].chosen.test_error <= result["resolution"]["tolerance"]
    assert result["class"].kind in ("ramp_like", "continuous")
    bounds = display_bounds(positions, texts)
    assert bounds == (20.0, 300.0)
    assert result["discrete"].family == "continuous"


def test_scenario_2_non_linear_displays_are_named_by_their_shape():
    positions = grid()
    # a fourth-power stage (the shape a synthesizer uses for an envelope time)
    power = texts_for(positions, lambda t: f"{32.0003 * t ** 4:.5f} s")
    power_result = analyze(positions, power)
    assert power_result["mapping"].chosen.kind == "power"
    assert power_result["mapping"].chosen.params["exponent"] == pytest.approx(4.0, abs=0.05)
    assert power_result["mapping"].chosen.profile_law_kind == "power"

    # an exponential display: 2 ** (-10 + 14 t)
    exponential = texts_for(positions, lambda t: f"{2.0 ** (-10.0 + 14.0 * t):.6f} s")
    exponential_result = analyze(positions, exponential)
    assert exponential_result["mapping"].chosen.kind == "exponential"
    assert exponential_result["resolution"]["relative_mode"] in ("magnitude", "absolute")


def test_scenario_2b_a_shape_that_does_not_fit_is_unknown_not_approximated():
    positions = grid()
    texts = texts_for(positions, lambda t: f"{math.sin(t * 12.0) * 50.0:.3f} Hz")
    result = analyze(positions, texts)
    assert result["mapping"].chosen is None
    assert result["mapping"].reason == "model_unstable"
    assert any("monotone" in note for note in result["mapping"].notes)


def test_scenario_3_units_are_recorded_as_the_display_states_them():
    positions = grid()
    texts = texts_for(positions, lambda t: f"{-80.0 + 80.0 * t:.1f} dB")
    result = analyze(positions, texts)
    assert result["resolution"]["state"] == "measured"
    assert result["mapping"].chosen.kind == "affine"


def test_scenario_9_an_unknown_range_says_why_instead_of_guessing():
    analysis = analyze_range()
    assert analysis.chosen is None
    assert analysis.reason == "api_does_not_expose"
    assert analysis.notes
    # the host's normalized 0..1 space is recorded but never published as a range
    spaces = [claim.get("detail", {}).get("space") for claim in analysis.claims]
    assert "normalized" in spaces


def test_scenario_10_conflicting_sources_keep_the_strongest_and_report_the_conflict():
    analysis = analyze_range(
        declared_native=(8.0, 136.0),
        display=(-52.0, 76.0),
        unit="semitones",
    )
    assert analysis.chosen == [8.0, 136.0]
    assert analysis.space == "native"
    assert analysis.conflict is not None
    assert analysis.conflict.rule == "strongest_widest_wins"
    # the two *range* claims disagree; the normalized addressing space is recorded
    # but never competes, which is why it is not among the conflict's claims
    assert len(analysis.conflict.claims) == 2
    assert [claim.source_kind for claim in analysis.conflict.claims] == ["declared", "measured"]


def test_display_space_ranges_are_labelled_as_display_space():
    analysis = analyze_range(display=(-52.0, 76.0), unit="semitones")
    assert analysis.chosen == [-52.0, 76.0]
    assert analysis.space == "display"
    assert any("display-space range" in note for note in analysis.notes)


def test_model_selection_prefers_the_simplest_admissible_shape():
    positions = grid()
    # an exact affine ramp: `table` and `piecewise_affine` also fit, but neither wins
    texts = texts_for(positions, lambda t: f"{t * 100.0:.2f}")
    result = analyze(positions, texts)
    assert result["mapping"].chosen.kind in ("affine", "identity")
    assert result["mapping"].chosen.complexity <= 2
    accepted = [fit.kind for fit in result["mapping"].fits if fit.accepted]
    assert "table" in accepted, "a lookup table does fit an exact ramp; it must not be chosen"
    assert result["mapping"].chosen.kind != "table"
    assert any("competing admissible fits" in note for note in result["mapping"].notes)


# ---------------------------------------------------------------------------
# 4-8, 11: discrete structure
# ---------------------------------------------------------------------------


def banded(labels: list[str], points: int = 17) -> tuple[list[float], list[str]]:
    positions = grid(points)
    count = len(labels)
    texts = [labels[min(count - 1, int(value * count))] for value in positions]
    return positions, texts


def test_scenario_4_enumeration_members_and_transitions_are_measured_not_sampled():
    positions, texts = banded(["12dB", "24dB", "Notch Blend", "Notch Spread", "B/P/N"])
    mapping = fit_mapping(positions, texts)
    store = ObservationStore(sampler=lambda index, value: texts[
        min(len(texts) - 1, max(0, int(value * len(texts))))
    ])
    analysis = analyze_discrete(
        index=0,
        positions=positions,
        texts=texts,
        mapping=mapping,
        store=store,
        refine_budget=400,
    )
    assert analysis.family == "enum"
    assert len(analysis.members) == 5
    assert analysis.state == "measured"
    # every transition was narrowed to an adjacent-double bracket by bisection
    states = {item.state for item in analysis.transitions}
    assert states <= {"exact"}, states
    for item in analysis.transitions:
        low, high = item.bracket
        assert math.nextafter(low, high) >= high
    assert all(member.interior_verified for member in analysis.members)
    classification = classify_control(positions=positions, texts=texts, discrete=analysis, mapping=mapping)
    assert classification.kind == "enum"
    assert classification.evidence["bands_verified"] == 5


def test_scenario_11_unnamed_states_are_kept_and_flagged():
    positions, texts = banded(["12dB", "24dB", "", ""])
    mapping = fit_mapping(positions, texts)
    analysis = analyze_discrete(index=0, positions=positions, texts=texts, mapping=mapping)
    assert analysis.family == "enum"
    assert analysis.unlabelled == [""]
    unlabelled = [member for member in analysis.members if member.unlabelled]
    assert len(unlabelled) == 1 and unlabelled[0].label == ""


def test_scenario_5_a_toggle_is_a_boolean_from_the_host_flag():
    positions, texts = banded(["normal", "bypassed"])
    steps = StepSizes(reported=True, step=1.0, is_toggle=True)
    mapping = fit_mapping(positions, texts)
    discrete = analyze_discrete(
        index=0, positions=positions, texts=texts, mapping=mapping, is_toggle=True, host_step=1.0
    )
    classification = classify_control(
        positions=positions, texts=texts, discrete=discrete, mapping=mapping, steps=steps
    )
    assert discrete.family == "boolean"
    assert classification.kind == "boolean" and classification.confidence == "strong"
    assert "toggle" in " ".join(classification.reasons)


def test_scenario_6_an_integer_ladder_needs_more_than_integers_in_the_display():
    """Printing integers is not evidence of integer storage: P24 measured that
    "the samples print integers" describes the sweep, not the display. The host's
    own step is the corroborating evidence, and it may be absent."""
    positions = grid(17)
    texts = [f"{index * 7}" for index in range(17)]  # a faithful integer ramp
    mapping = fit_mapping(positions, texts)

    without_step = analyze_discrete(index=0, positions=positions, texts=texts, mapping=mapping)
    assert without_step.family == "continuous"
    assert without_step.reason == "requires_setter_experiment"
    assert any("integer" in note for note in without_step.notes)

    with_step = analyze_discrete(
        index=0, positions=positions, texts=texts, mapping=mapping, host_step=1.0
    )
    assert with_step.family == "integer_ladder"
    classification = classify_control(
        positions=positions,
        texts=texts,
        discrete=with_step,
        mapping=mapping,
        steps=StepSizes(reported=True, step=1.0),
    )
    assert classification.kind == "integer_like"
    assert classification.evidence["requires"] == "setter_experiment"


def test_scenario_7_a_faithful_numeric_ramp_is_never_an_enumeration():
    positions = grid(65)
    texts = texts_for(positions, lambda t: f"{t * 100.0:.4f}")
    mapping = fit_mapping(positions, texts)
    evidence = sampled_ramp_evidence(
        positions, texts, mapping=mapping, resolution_tolerance=1e-3
    )
    assert evidence["is_ramp"] and evidence["reasons"] == []
    analysis = analyze_discrete(index=0, positions=positions, texts=texts, mapping=mapping)
    assert analysis.family == "continuous"
    classification = classify_control(positions=positions, texts=texts, discrete=analysis, mapping=mapping)
    assert classification.kind in ("ramp_like", "continuous")
    assert classification.kind != "enum"


def test_a_banded_index_series_is_not_mistaken_for_a_ramp():
    """The P23/P24 trap in the other direction: a banded member list must fail the
    ramp criterion, or a real enumeration would be re-labelled as a ramp."""
    positions, texts = banded(["a", "b", "c", "d"])
    mapping = fit_mapping(positions, texts)
    evidence = sampled_ramp_evidence(positions, texts, mapping=mapping, resolution_tolerance=0.5)
    assert not evidence["is_ramp"]
    assert any("not a number" in reason for reason in evidence["reasons"])


def test_scenario_8_a_rounded_display_stays_numeric_and_is_flagged_as_rounding():
    positions = grid(17)
    # a display with 0 decimals over a 7-unit span: eleven samples repeat a text
    texts = [f"{value * 7.0:.0f} Hz" for value in positions]
    assert len(set(texts)) < len(texts)
    mapping = fit_mapping(positions, texts)
    analysis = analyze_discrete(index=0, positions=positions, texts=texts, mapping=mapping)
    classification = classify_control(positions=positions, texts=texts, discrete=analysis, mapping=mapping)
    assert classification.kind == "stepped", classification.reasons
    assert "rounding" in " ".join(classification.reasons)
    assert "enum" in classification.alternatives


def test_two_labels_from_two_samples_are_not_enough_to_call_a_two_state_control():
    """Found by the P26 independent review: with two samples, a two-state control and
    the first two members of a multi-member enumeration look the same, so the class
    is the weaker one and the sample count is the recorded reason."""
    texts = ["Sine", "Triangle"]
    positions = [0.0, 1.0]
    classification = classify_control(positions=positions, texts=texts, discrete=None, mapping=None)
    assert classification.kind == "enum" and classification.confidence == "weak"
    assert classification.evidence["samples"] == 2
    assert "boolean" in classification.alternatives

    enough = ["Sine", "Triangle", "Sine"]
    classification = classify_control(
        positions=[0.0, 0.5, 1.0], texts=enough, discrete=None, mapping=None
    )
    assert classification.kind == "boolean"
    assert "enum" in classification.alternatives


def test_a_toggle_flag_still_wins_over_the_sample_count():
    texts = ["off", "on"]
    steps = StepSizes(reported=True, step=1.0, is_toggle=True)
    classification = classify_control(
        positions=[0.0, 1.0], texts=texts, discrete=None, mapping=None, steps=steps
    )
    assert classification.kind == "boolean" and classification.confidence == "strong"


def test_enum_members_are_reported_as_a_lower_bound():
    positions, texts = banded(["a", "b", "c"])
    mapping = fit_mapping(positions, texts)
    discrete = analyze_discrete(index=0, positions=positions, texts=texts, mapping=mapping)
    classification = classify_control(positions=positions, texts=texts, discrete=discrete, mapping=mapping)
    assert classification.kind == "enum"
    assert classification.evidence["members_are_a_lower_bound"] is True
    assert any("lower bound" in reason for reason in classification.reasons)


def test_a_budget_that_runs_out_marks_the_result_incomplete():
    positions, texts = banded(["a", "b", "c", "d", "e", "f"])
    mapping = fit_mapping(positions, texts)
    store = ObservationStore(sampler=lambda index, value: "x")
    analysis = analyze_discrete(
        index=0,
        positions=positions,
        texts=texts,
        mapping=mapping,
        store=store,
        refine_budget=1,
    )
    assert analysis.budget_exhausted is True
    assert analysis.state == "budget_exhausted"
    assert any(item.state == "not_attempted" for item in analysis.transitions)


def test_without_a_sampler_the_grid_edges_are_reported_as_upper_bounds():
    positions, texts = banded(["low", "high"])
    analysis = analyze_discrete(index=0, positions=positions, texts=texts)
    assert analysis.state == "sampled_upper_bounds"
    assert all(item.state == "sampled_upper_bound" for item in analysis.transitions)
    assert all("sampled grid" in (item.reason or "") for item in analysis.transitions)


def test_no_observations_is_an_explicit_reason_not_an_empty_control():
    analysis = analyze_discrete(index=0, positions=[], texts=[])
    assert analysis.family == "unknown" and analysis.reason == "no_observations"
    classification = classify_control(positions=[], texts=[], discrete=analysis, mapping=None)
    assert classification.kind == "unknown" and classification.confidence == "none"
