"""P19 — enum writes must land in the member's *own* band (offline, registry-derived).

**The defect this file exists for (measured live, 2026-10-11).** The profile
trusted the registry's `enum_index` declaration, whose conversion is
``index / (n - 1)`` - uniformly spaced bands. The plugin's own sweeps say the
bands are not uniform. `Filter 1 Style`'s "24dB" occupies ``[0.0625, 0.1875)``
while the uniform mapping writes **0.25**, which is inside **"Notch Blend"**'s
band. The live consequence: ``plugin_write(filter_1_style="24dB")`` committed,
verified `ALL_VERIFIED`, and the layer's own read-back said "24dB" - because the
same wrong arithmetic ran in both directions - while REAPER's formatted value at
that parameter read **"Notch Blend"**. Only the *independent* channel disagreed.

**What is pinned here.** The rule (`profile_build._declared_enum_positions`)
attaches measured band positions **only** when the declared uniform mapping puts
at least one member outside its own measured band, and the expected set is
re-derived in this test from the registry's sweeps rather than copied from a
constant - so the test cannot drift with the implementation. The counter-example
is pinned too: the controls whose uniform mapping *is* correct (`distortion_type`'s
bands happen to bracket the uniform points, and the two-state switches) must
carry **no** override, which is what keeps the change additive.
"""

from __future__ import annotations

import pytest

from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.providers.vital import (
    DEFAULT_REGISTRY_DIR,
    VitalProvider,
    load_registry,
)
from reaper_reapy_mcp.plugins.providers.vital import depth as depth_module

IDENTITY = {
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "Vital.vst3",
    "param_count": 2858,
    "version": "1.5.5",
}

#: The tolerance the profile uses, restated here so the test's re-derivation is
#: independent of the implementation's copy (the sweep stores change points
#: rounded to ~7 decimals).
TOLERANCE = 1e-5


@pytest.fixture(scope="module")
def registry_and_profile():
    try:
        registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception as exc:  # noqa: BLE001 - an absent registry is a skip
        pytest.skip(f"the registry is not present: {exc}")
    profile = VitalProvider(registry=registry).build_profile(dict(IDENTITY))
    return registry, profile


def band_spans(states: list[tuple[float, str]]) -> list[tuple[str, float, float]]:
    """The member bands, re-derived here (left edge, right edge exclusive)."""
    points: list[tuple[float, str]] = []
    for norm, display in sorted(states, key=lambda pair: pair[0]):
        label = str(display).strip()
        if points and points[-1][1] == label:
            continue
        points.append((float(norm), label))
    spans: list[tuple[str, float, float]] = []
    for index, (start, label) in enumerate(points):
        if index + 1 < len(points):
            end = points[index + 1][0]
        else:
            end = min(1.0, start + (start - points[index - 1][0] if index else 1.0))
        spans.append((label, start, end))
    return spans


def expected_corrected_ids(registry) -> dict[str, int]:
    """Which declared enums the uniform mapping gets *wrong*, from the sweeps.

    This is the test's own derivation, deliberately written against the registry
    data rather than against the profile: the members are the distinct non-empty
    labels in the order the sweep meets them (the same rule `_enum_values` uses),
    and a member counts as wrong when its uniform position
    (``index / (n - 1)``) falls outside its own measured band.
    """
    scales = registry.scales()
    out: dict[str, int] = {}
    for row in registry.rows():
        if row.get("kind") != "enum_index":
            continue
        states = depth_module.states_of(scales, str(row.get("name") or row.get("state_key"))) or []
        members: list[str] = []
        for _norm, display in sorted(states, key=lambda pair: pair[0]):
            label = str(display).strip()
            if label and label not in members:
                members.append(label)
        if len(members) < 2:
            continue
        spans = [span for span in band_spans(states) if span[0]]
        if len(spans) != len(members) or [label for label, _s, _e in spans] != members:
            continue
        n = len(members)
        outside = 0
        for index, (_label, start, end) in enumerate(spans):
            value = index / (n - 1)
            if not (start - TOLERANCE <= value <= end + TOLERANCE):
                outside += 1
        if outside:
            out[str(row.get("state_key"))] = outside
    return out


def test_the_corrected_set_is_exactly_what_the_sweeps_say(registry_and_profile):
    registry, profile = registry_and_profile
    expected = expected_corrected_ids(registry)
    assert expected, "the derivation itself found nothing - the test would be vacuous"
    actual = {
        str(control["id"]): int(
            ((control.get("evidence") or {}).get("positions_correction") or {}).get(
                "members_the_declared_mapping_puts_outside_their_band", 0
            )
        )
        for control in profile["controls"]
        if (control.get("evidence") or {}).get("positions_correction")
    }
    assert actual == expected, sorted(set(actual) ^ set(expected))


def test_the_measured_positions_are_inside_their_own_bands(registry_and_profile):
    """Every override must be a *write target* inside the member's measured band -
    the property that makes the write land where it says."""
    registry, profile = registry_and_profile
    scales = registry.scales()
    checked = 0
    for control in profile["controls"]:
        note = (control.get("evidence") or {}).get("positions_correction")
        if not note:
            continue
        spans = [span for span in band_spans(depth_module.states_of(scales, str(control["label"])) or []) if span[0]]
        positions = (control.get("law") or {}).get("params", {}).get("positions")
        assert positions is not None, control["id"]
        assert len(positions) == len(spans), control["id"]
        for position, (_label, start, end) in zip(positions, spans):
            assert start - TOLERANCE <= position <= end + TOLERANCE, (control["id"], position, start, end)
        checked += 1
    assert checked == len(expected_corrected_ids(registry)), checked


def test_the_two_state_switches_and_the_already_correct_enums_are_untouched(registry_and_profile):
    """The additive half: a declared enum whose uniform mapping is right keeps the
    declaration byte-identical. `distortion_type`'s bands happen to bracket the
    uniform points (0.2/0.4/0.6/0.8) and the switches' are [0, 0.5) / [0.5, 1]."""
    _registry, profile = registry_and_profile
    for control_id in ("distortion_type", "filter_1_on", "filter_2_on", "legato", "portamento_force", "filter_1_model", "pitch_bend_range"):
        control = next(row for row in profile["controls"] if row["id"] == control_id)
        params = dict((control.get("law") or {}).get("params") or {})
        assert "positions" not in params, (control_id, params.get("positions"))
        assert not (control.get("evidence") or {}).get("positions_correction"), control_id


def test_the_filter_style_now_writes_its_own_band_the_measured_defect(registry_and_profile):
    """The concrete defect, as an executable measurement: "24dB" no longer writes
    0.25 (which REAPER formatted as "Notch Blend") but a point inside [0.0625,
    0.1875) - and the round trip stays consistent."""
    _registry, profile = registry_and_profile
    control = next(row for row in profile["controls"] if row["id"] == "filter_1_style")
    uniform_would_be = 1 / (len(control["enum_values"]) - 1)
    assert uniform_would_be == pytest.approx(0.25)
    native = V.to_native(control, "24dB")
    normalized = V.to_normalized(control, native)
    assert normalized == pytest.approx(0.125, abs=1e-6), normalized
    assert 0.0625 <= normalized < 0.1875, normalized
    assert V.from_native(control, V.normalized_to_native(control, normalized)) == "24dB"
    # and the neighbouring member is no longer reachable by writing "24dB"
    notched = V.to_normalized(control, V.to_native(control, "Notch Blend"))
    assert notched == pytest.approx(0.25, abs=1e-6), notched
    assert normalized != pytest.approx(notched, abs=1e-6)


def test_the_read_leg_resolves_every_measured_grid_point_of_the_host(registry_and_profile):
    """**The read leg, against the host itself.** An independent review of
    2026-10-11 found that inverting an enum by *nearest midpoint* mis-resolves the
    values at a measured switch point (the host labels 0.0625 as "24dB"; the
    midpoint distance called it the member below). The grid below is the host's
    own labelling of that parameter, recorded live and extracted **verbatim** into
    `fixtures/vital_host_bands/filter_1_style_grid.json` (the three P19 recordings
    agree point for point and the fixture carries all three digests). Since P32 the
    test reads that deterministic fixture, never the newest file in the recorded
    run archive - the layer's inversion is compared against the measurement point
    by point, with the counter-example (nearest midpoint) recomputed here so the
    reason for `switch_points` stays a measurement."""
    from pathlib import Path

    import json

    fixture = Path(__file__).resolve().parent / "fixtures" / "vital_host_bands" / "filter_1_style_grid.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    assert document["control"] == "filter_1_style"
    assert document["recorded_by"]["grids_agree"] is True, document["recorded_by"]
    assert document["counts"]["points"] == len(document["grid"])
    host_labels = {float(normalized): display for normalized, display in document["grid"]}
    assert len(host_labels) >= 33, len(host_labels)
    _registry, profile = registry_and_profile
    control = next(row for row in profile["controls"] if row["id"] == "filter_1_style")
    positions = control["law"]["params"]["positions"]
    labelled = {value: label for value, label in host_labels.items() if label}
    tail = {value: label for value, label in host_labels.items() if not label}
    assert len(labelled) >= 30, len(labelled)  # non-vacuity: the bands are the subject
    assert tail, "the unlabelled tail is part of this build's measured map"
    disagreements = 0
    for normalized, expected in labelled.items():
        native = V.normalized_to_native(control, float(normalized))
        assert V.from_native(control, native) == expected, (normalized, expected, V.from_native(control, native))
        naive = min(range(len(positions)), key=lambda i: abs(positions[i] - float(normalized)))
        if V.from_native(control, naive) != expected:
            disagreements += 1
    assert disagreements >= 1, "the counter-example must still exist, or this test says nothing"
    # the *named gap*: this build's sixth filter style formats as an empty string, so
    # no member can be named for [0.5625, 1.0) - the layer answers with the last
    # labelled member, and that substitution is asserted here rather than discovered
    # later as a surprise. Recorded in the profile as `unlabelled_spans_in_the_sweep`.
    for normalized in tail:
        native = V.normalized_to_native(control, float(normalized))
        assert V.from_native(control, native) == "B/P/N", (normalized, V.from_native(control, native))
