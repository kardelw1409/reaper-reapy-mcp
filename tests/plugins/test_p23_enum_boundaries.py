"""P23 tests, workstream A: measured enum band boundaries (adaptive read-only discovery).

The channel under test is the one P22 left unfinished. P22 put band boundaries into
the profile from the registry's **sampled sweep**, whose change points are only upper
bounds on the true transition, at the sampling step's resolution (measured: 5,320 of
5,671 boundaries sit up to 3.35e-3 away from the uniform arithmetic, and every one of
those distances is *inside* that resolution). The host formatter,
``TrackFX_FormatParamValueNormalized``, accepts an arbitrary double and prints what
the host would display for it, so the transition can be located by bisection - to
adjacent doubles, when the caller asks for it.

Two layers of testing:

- **unit**: ``apply_enum_boundaries`` on hand-built controls, with synthetic artifacts
  for the classes the fixture cannot host (a position defect, a member-list mismatch,
  a non-advancing boundary, a hostile numeric);
- **pinned**: the real measured brackets of the contract-relevant controls, from
  ``fixtures/vital_enum_boundaries/vital_enum_boundaries.json`` - the numbers there are
  the host's own answers at real values, not invented ones.

The safety claim is asserted structurally, not promised: the module imports no host
module, the merge takes no bridge, and the probe source contains no setter call.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import IncompatibleSettings
from reaper_reapy_mcp.plugins.providers.vital import enum_boundaries as EB
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"
ARTIFACT_FIXTURE = FIXTURES / "vital_enum_boundaries" / "vital_enum_boundaries.json"
PROBE = Path(__file__).resolve().parent / "live" / "probe_p23_enum_boundaries.py"

IDENTITY = {
    "fx_name": PB.VITAL_FX_NAME,
    "class_id": PB.VITAL_CLASS_ID,
    "module": PB.VITAL_MODULE,
    "param_count": 8,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def control(control_id: str, members, *, positions=None, switch=None, unit=None) -> dict:
    params: dict = {}
    if positions is not None:
        params["positions"] = list(positions)
    if switch is not None:
        params["switch_points"] = list(switch)
    return {
        "id": control_id,
        "label": control_id,
        "module": None,
        "type": "enum",
        "unit": unit,
        "range": {"min": 0.0, "max": float(len(members) - 1)},
        "discrete": True,
        "enum_values": [{"id": member, "label": member} for member in members],
        "aliases": [],
        "authority": {"read": "parameter", "write": "parameter"},
        "binding": {"index": 1, "ident": None, "state_key": control_id},
        "law": {"kind": "enum_index", "params": params},
        "evidence": {"strength": "strong"},
    }


def artifact(tmp_path: Path, controls: dict, *, tolerance: float = 1e-18, name: str = "a.json") -> Path:
    payload = {
        "kind": EB.ENUM_BOUNDARIES_KIND,
        "method_version": EB.ENUM_BOUNDARIES_METHOD_VERSION,
        "generated_utc": "2026-01-01T00:00:00Z",
        "host": {"name": "reaper"},
        "identity": dict(IDENTITY),
        "method": {"call": "TrackFX_FormatParamValueNormalized", "tolerance": tolerance},
        "controls": controls,
        "read_only_proof": {"values_unchanged": True, "chunk_sha256_unchanged": True},
    }
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def measured(members, brackets, *, state: str = "exact", tail=None) -> dict:
    """A control record whose brackets are ``(lo, hi)`` pairs, one per transition."""
    return {
        "index": 1,
        "members": list(members),
        "n_members": len(members),
        "current_switch_points": [],
        "current_source": "uniform",
        "transitions": [
            {
                "i": position,
                "state": state,
                "lo": lo,
                "hi": hi,
                "lower_member": members[position - 1],
                "upper_member": members[position],
            }
            for position, (lo, hi) in enumerate(brackets, start=1)
        ],
        "positions_checked": [],
        "unresolved": [],
        "tail": tail,
    }


def merged(tmp_path: Path, controls: list[dict], records: dict, **kwargs):
    path = artifact(tmp_path, records, **kwargs)
    loaded = EB.load_enum_boundaries(path)
    report = EB.apply_enum_boundaries(controls, loaded)
    return controls, report, loaded


# ---------------------------------------------------------------------------
# 1. The merge: what a measurement may and may not change
# ---------------------------------------------------------------------------


def test_a_measured_bracket_that_contradicts_the_profile_replaces_it(tmp_path):
    """The sweep's change point is an *upper bound*; the bracket is the measurement.

    Pinned on the real data: ``osc_1_destination``'s second band starts at
    1/28 = 0.035714285714..., while the profile carried 0.0390625 (the first
    sample of a 1/256 grid that showed the new member). Values in between read the
    lower member through the profile and the upper one on the host's own display.
    """
    controls = [control("osc_1_destination", ["a", "b", "c"], switch=[0.0, 0.0390625, 0.109375])]
    records = {
        "osc_1_destination": measured(
            ["a", "b", "c"],
            [(0.03571428504370153, 0.03571428585860133), (0.10714285427423567, 0.10714285479810834)],
        )
    }
    controls, report, _ = merged(tmp_path, controls, records)
    params = controls[0]["law"]["params"]
    assert params["switch_points"][0] == 0.0
    assert params["switch_points"][1] == 0.03571428585860133, (
        "the measured hi (the smallest double that reads as the upper member) replaces "
        "the sweep's upper bound"
    )
    assert params["switch_points"][2] == 0.10714285479810834
    evidence = params["boundary_evidence"]
    assert evidence["previous_source"] == "switch_points"
    assert evidence["states"] == ["exact"]
    assert evidence["transitions"] == 2
    assert evidence["widest_offset_before"] == pytest.approx(0.0390625 - 0.03571428585860133, rel=1e-6)
    assert report["counts"]["controls_refined"] == 1
    # and the read path now agrees with the host at the value that used to be misread
    assert V.normalized_to_native(controls[0], 0.037) == 1.0
    assert V.normalized_to_native(controls[0], math.nextafter(params["switch_points"][1], -math.inf)) == 0.0


def test_a_bracket_that_contains_the_profiles_boundary_changes_nothing(tmp_path):
    """A measurement that does not contradict the profile leaves it byte-identical.

    This is the minimal-diff rule: the evidence lives in the artifact (a declared
    source), and the profile only changes where a *decision* would differ.
    """
    controls = [control("sw", ["Off", "On"])]
    before = json.dumps(controls[0], sort_keys=True)
    records = {"sw": measured(["Off", "On"], [(0.49999999, 0.50000001)])}
    controls, report, _ = merged(tmp_path, controls, records)
    assert json.dumps(controls[0], sort_keys=True) == before, (
        "a bracket containing the profile's own 0.5 must not rewrite the profile"
    )
    assert report["counts"]["controls_confirmed"] == 1
    assert report["counts"]["controls_refined"] == 0


def test_a_ramping_display_is_named_and_its_geometry_left_alone(tmp_path):
    """``distortion_filter_blend``: 257 "members" that are really a sampled ramp.

    The host prints a value *between* two of them, so there is no band structure to
    find. The profile keeps the geometry it had and gains a note saying why - the
    finding is recorded, not invented around.
    """
    members = [str(index / 128.0) for index in range(3)]
    controls = [control("ramp", members)]
    before = json.dumps(controls[0], sort_keys=True)
    records = {"ramp": measured(members, [(0.1, 0.2), (0.4, 0.5)], state="continuous_display")}
    controls, report, _ = merged(tmp_path, controls, records)
    assert json.dumps(controls[0], sort_keys=True) != before
    params = controls[0]["law"]["params"]
    assert "switch_points" not in params, "the geometry must not change"
    assert params["boundary_evidence"]["state"] == "no_band_structure"
    assert "ramp" in [row["control"] for row in report["unresolved_band_structures"]]
    assert report["counts"]["controls_without_band_structure"] == 1
    assert report["counts"]["controls_refused"] == 0, "it is a finding, not a refusal"


def test_an_unmeasurable_control_is_refused_and_keeps_its_geometry(tmp_path):
    """``bypass``: the host never prints either member id, so no boundary exists.

    A half-measured map is the state this layer must never carry, so one
    unmeasurable transition refuses the *whole control*.
    """
    controls = [control("bypass", ["normal", "bypassed"])]
    before = json.dumps(controls[0], sort_keys=True)
    records = {"bypass": measured(["normal", "bypassed"], [(0.0, 0.5)], state="unresolved")}
    controls, report, _ = merged(tmp_path, controls, records)
    params = controls[0]["law"]["params"]
    assert "switch_points" not in params
    assert params["boundary_evidence"]["state"] == "undetermined"
    assert report["counts"]["controls_refused"] == 1
    assert report["refusals"][0]["reason"] == "not_every_transition_is_measured"
    assert report["counts"]["controls_without_band_structure"] == 0
    # the law kind and the member list are untouched: only geometry could have moved
    assert controls[0]["law"]["kind"] == "enum_index"
    assert len(controls[0]["enum_values"]) == 2
    assert before is not None


def test_a_member_list_that_disagrees_with_the_profile_is_refused(tmp_path):
    controls = [control("sw", ["Off", "On"])]
    records = {"sw": measured(["Off", "On", "Third"], [(0.2, 0.3), (0.6, 0.7)])}
    controls, report, _ = merged(tmp_path, controls, records)
    assert report["refusals"][0]["reason"] == "member_list_differs_from_the_profile"
    assert "switch_points" not in controls[0]["law"]["params"]


def test_an_unlabelled_tail_caps_the_last_members_band(tmp_path):
    """``filter_1_style``: above 0.5625 the host prints nothing.

    The last *named* member's band ends there, which is where its midpoint (and so
    its write target) must come from - the previous code mirrored the band before
    it, which is right only when nothing follows.
    """
    controls = [control("style", ["12dB", "24dB", "Notch"])]
    records = {
        "style": measured(
            ["12dB", "24dB", "Notch"],
            [(0.1, 0.100000000000000005), (0.3, 0.300000000000000044)],
            tail={
                "state": "unlabelled_region",
                "lo": 0.562499969266355,
                "hi": 0.5624999701976776,
                "text": "",
            },
        )
    }
    controls, report, _ = merged(tmp_path, controls, records)
    evidence = controls[0]["law"]["params"]["boundary_evidence"]
    assert len(report["unlabelled_regions"]) == 1
    region = report["unlabelled_regions"][0]
    assert (region["control"], region["lo"], region["hi"], region["text"]) == (
        "style",
        0.562499969,
        0.562499970,
        "",
    )
    # P24: the region is left *unnamed on purpose*, and the reason travels with it -
    # the plugin's own string table has fewer entries than the parameter has states
    assert "no name to print" in region["why"]
    assert "kFilterStyleNames" in region["source"]
    assert evidence["transitions"] == 2
    # the write target is the midpoint of the measured band, not of a mirrored one
    assert controls[0]["law"]["params"]["positions"][2] == pytest.approx(
        (0.300000000000000044 + 0.5624999701976776) / 2, abs=1e-9
    )


def test_a_write_target_outside_its_measured_band_moves_and_is_reported(tmp_path):
    """P19's class of defect, now detected mechanically: a target in the wrong band."""
    controls = [
        control("c", ["A", "B", "C"], positions=[0.0, 0.9, 1.0])
    ]  # 0.9 is inside C's band, not B's
    records = {"c": measured(["A", "B", "C"], [(0.2, 0.20000000000000004), (0.6, 0.6000000000000001)])}
    controls, report, _ = merged(tmp_path, controls, records)
    positions = controls[0]["law"]["params"]["positions"]
    assert positions[1] == pytest.approx((0.20000000000000004 + 0.6000000000000001) / 2, abs=1e-9)
    assert [row["member"] for row in report["positions_moved"]] == ["B"]
    assert report["positions_moved"][0]["reason"] == "the position is outside the measured band"


def test_a_position_that_displays_another_member_is_moved_and_flagged(tmp_path):
    controls = [control("c", ["A", "B"], positions=[0.0, 1.0])]
    record = measured(["A", "B"], [(0.3, 0.30000000000000004)])
    record["positions_checked"] = [
        {"member": "A", "position": 0.0, "text": "A", "ok": True},
        {"member": "B", "position": 1.0, "text": "A", "ok": False},
    ]
    controls, report, _ = merged(tmp_path, controls, {"c": record})
    assert report["position_defects"] == [{"control": "c", "member": "B", "position": 1.0}]
    assert report["positions_moved"][0]["reason"] == "the position displays another member"


def test_a_control_the_profile_does_not_have_is_ignored(tmp_path):
    controls = [control("sw", ["Off", "On"])]
    records = {"no_such_control": measured(["x", "y"], [(0.4, 0.5)])}
    controls, report, _ = merged(tmp_path, controls, records)
    assert report["counts"]["controls_seen"] == 0
    assert report["counts"]["controls_refined"] == 0


# ---------------------------------------------------------------------------
# 2. The loader: every malformed artifact is a typed refusal
# ---------------------------------------------------------------------------


def test_the_pinned_fixture_loads_and_carries_the_measured_brackets():
    loaded = EB.load_enum_boundaries(ARTIFACT_FIXTURE)
    assert loaded.method_version == EB.ENUM_BOUNDARIES_METHOD_VERSION
    assert loaded.matches_identity(dict(IDENTITY))
    chorus_on = loaded.controls["chorus_on"]
    bracket = chorus_on["transitions"][0]
    # both forms are exact-level claims: `exact` brackets the transition between
    # adjacent doubles by bisection, `exact_at` proves the same double in two calls
    # (the candidate itself flips the display and the double below it does not). Which
    # one a run produces depends on whether the profile already carried the boundary.
    assert bracket["state"] in ("exact", "exact_at")
    assert bracket["hi"] == 0.4999999850988388, (
        "the host flips at 0.5 - 1.49e-8, not at 0.5: the measured value is the whole "
        "point of the channel"
    )
    assert math.nextafter(bracket["lo"], math.inf) == bracket["hi"], "adjacent doubles"
    assert loaded.controls["bypass"]["transitions"][0]["state"] == "unresolved"
    assert loaded.controls["filter_1_style"]["tail"]["text"] == ""


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda payload: payload.update({"kind": "something-else"}), "kind"),
        (lambda payload: payload.update({"method_version": 99}), "method_version"),
        (lambda payload: payload.pop("identity"), "identity"),
        (lambda payload: payload.update({"method": {}}), "tolerance"),
        (lambda payload: payload.update({"method": {"tolerance": 0.0}}), "tolerance"),
        (lambda payload: payload.update({"method": {"tolerance": float("nan")}}), "tolerance"),
        (lambda payload: payload.update({"controls": {}}), "controls"),
        (lambda payload: payload["controls"]["sw"].update({"members": ["only-one"]}), "member list"),
        (lambda payload: payload["controls"]["sw"].update({"transitions": []}), "transitions"),
        (lambda payload: payload["controls"]["sw"]["transitions"][0].update({"i": 5}), "i=5"),
        (lambda payload: payload["controls"]["sw"]["transitions"][0].update({"state": "maybe"}), "state"),
        (lambda payload: payload["controls"]["sw"]["transitions"][0].update({"hi": None}), "bracket"),
        (lambda payload: payload["controls"]["sw"]["transitions"][0].update({"lo": 0.9}), "bracket"),
        (lambda payload: payload["controls"]["sw"]["transitions"][0].update({"hi": float("inf")}), "bracket"),
        (
            lambda payload: payload["controls"]["sw"]["transitions"][0].update({"upper_member": "Nope"}),
            "members it separates",
        ),
        (
            lambda payload: payload["controls"].update(
                {
                    "three": measured(
                        ["a", "b", "c"], [(0.2, 0.20000000000000004), (0.1, 0.100000000000000005)]
                    )
                }
            ),
            "advance",
        ),
    ],
)
def test_a_malformed_artifact_is_a_typed_refusal(tmp_path, mutate, fragment):
    records = {"sw": measured(["Off", "On"], [(0.49, 0.51)])}
    path = artifact(tmp_path, records)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        EB.load_enum_boundaries(path)
    assert fragment in excinfo.value.message, excinfo.value.message


def test_an_oversized_artifact_is_refused_before_it_is_parsed(tmp_path, monkeypatch):
    path = artifact(tmp_path, {"sw": measured(["Off", "On"], [(0.49, 0.51)])})
    monkeypatch.setattr(EB, "_MAX_ARTIFACT_BYTES", 32)
    with pytest.raises(IncompatibleSettings) as excinfo:
        EB.load_enum_boundaries(path)
    assert "beyond the" in excinfo.value.message
    monkeypatch.undo()

    long_member = tmp_path / "long.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["controls"]["sw"]["members"] = ["Off", "x" * 5000]
    payload["controls"]["sw"]["transitions"][0]["upper_member"] = "x" * 5000
    long_member.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        EB.load_enum_boundaries(long_member)
    assert "longer than" in excinfo.value.message


def test_an_artifact_with_too_many_controls_is_refused(tmp_path, monkeypatch):
    records = {f"c{index}": measured(["a", "b"], [(0.4, 0.5)]) for index in range(4)}
    path = artifact(tmp_path, records)
    monkeypatch.setattr(EB, "_MAX_CONTROLS", 2)
    with pytest.raises(IncompatibleSettings) as excinfo:
        EB.load_enum_boundaries(path)
    assert "beyond the bound" in excinfo.value.message


def test_an_unreadable_or_unparsable_file_is_a_typed_refusal(tmp_path):
    with pytest.raises(IncompatibleSettings):
        EB.load_enum_boundaries(tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        EB.load_enum_boundaries(broken)
    assert "does not parse" in excinfo.value.message


def test_an_artifact_from_another_build_is_loaded_but_does_not_match():
    loaded = EB.load_enum_boundaries(ARTIFACT_FIXTURE)
    other = {**IDENTITY, "param_count": 2858}
    assert loaded.matches_identity(other) is False


def test_the_environment_override_resolves_and_an_empty_value_is_an_error(tmp_path):
    assert EB.resolve_enum_boundaries_path(str(tmp_path / "x.json"), {}) == str(
        (tmp_path / "x.json").resolve()
    )
    with pytest.raises(IncompatibleSettings):
        EB.resolve_enum_boundaries_path(None, {EB.ENUM_BOUNDARIES_ENV: "  "})
    explicit = EB.resolve_enum_boundaries_path(None, {EB.ENUM_BOUNDARIES_ENV: str(tmp_path / "y.json")})
    assert explicit.endswith("y.json")


# ---------------------------------------------------------------------------
# 3. End to end through the provider, on the pinned fixture artifact
# ---------------------------------------------------------------------------


def fixture_provider(**kwargs) -> VitalProvider:
    return VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(ARTIFACT_FIXTURE),
        **kwargs,
    )


@pytest.fixture(scope="module")
def measured_profile() -> dict:
    return VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        enum_boundaries_path=str(ARTIFACT_FIXTURE),
    ).build_profile(dict(IDENTITY))


def test_the_profile_carries_the_measured_geometry_and_names_the_artifact(measured_profile):
    block = measured_profile["enum_boundaries"]
    assert block["state"] == "applied" and block["applied"] is True
    assert block["artifact"]["file"] == "vital_enum_boundaries.json"
    assert len(block["artifact"]["sha256"]) == 64
    assert block["tolerance"] == 1e-18
    assert "read-only" in block["call"]
    # the fixture registry declares exactly two enums: chorus_tempo (11 members,
    # measured) and distortion_filter_blend (257 members, a ramping display).
    # Since P24 the ramp is a *number* in the profile, so the artifact's rows split:
    # one still-enum control is refined with measured bands, one is cross-checked as
    # a control the profile no longer models as an enum.
    assert block["counts"]["controls_measured"] == 1
    assert block["counts"]["controls_without_band_structure"] == 1, (
        "artifact-derived: the artifact measured a ramp display for one control"
    )
    assert block["counts"]["controls_not_enums_any_more"] == 1, (
        "profile-derived: that control is a numeric law now, so it has no bands to place"
    )
    assert block["controls_refined"] == ["chorus_tempo"]
    assert [row["control"] for row in block["not_enums_any_more"]] == [
        "distortion_filter_blend"
    ]
    assert block["not_enums_any_more"][0]["profile_model"] == {
        "type": "number",
        "law": "affine",
    }
    assert block["without_band_structure"] == [], (
        "nothing is left with a member list and no bands: the ramp is a number now"
    )
    # every counter that could be misread is explained where it is read (P24 added
    # `confirmations_withheld`, whose name would otherwise read as "the artifact
    # confirmed nothing" rather than "these conversions the artifact could not
    # confirm" - so the block explains each one and this pins the *set*)
    assert sorted(block["counts_note"]) == [
        "confirmations_withheld",
        "controls_not_enums_any_more",
        "controls_without_band_structure",
    ], "the counters that could be misread are explained where they are read"
    assert [row["file"] for row in measured_profile["provenance"]["sources"]][-1] == (
        "vital_enum_boundaries.json"
    )
    # the *evidence levels* are spelled out where a reader will look for them
    assert "exact" in block["evidence_levels"]
    assert "continuous_display" in block["evidence_levels"]


def test_the_fixture_registrys_two_enums_are_measured_and_handled_separately(measured_profile):
    controls = {str(control["id"]): control for control in measured_profile["controls"]}
    tempo = controls["chorus_tempo"]
    params = tempo["law"]["params"]
    assert params["switch_points"][1] == 0.04999999888241292, (
        "chorus_tempo's first switch point is measured, not the uniform 0.05"
    )
    assert set(params["boundary_evidence"]["states"]) <= {"exact", "exact_at"}
    assert params["boundary_evidence"]["band_interiors_verified"] == (
        params["boundary_evidence"]["band_interiors_checked"]
    )
    assert len(params["boundary_evidence"]["band_ends"]) == len(tempo["enum_values"]) + 1
    assert V.normalized_to_native(tempo, 0.04999999888241292) == 1.0
    assert V.normalized_to_native(tempo, math.nextafter(0.04999999888241292, -math.inf)) == 0.0

    blend = controls["distortion_filter_blend"]
    # P24: the ramp is a *number* with a measured affine law, and the artifact's own
    # measurement of it (a display between members at all 256 transitions) is
    # carried on the law as the cross-check that agrees with the sweep
    assert blend["type"] == "number" and blend["enum_values"] is None
    assert blend["law"]["kind"] == "affine"
    assert "switch_points" not in (blend["law"].get("params") or {})
    confirmation = blend["law"]["params"]["artifact_confirmation"]
    assert confirmation["state"] == "continuous_display"
    assert confirmation["declared_members"] == 257
    assert confirmation["transitions_measured"] == 256
    assert confirmation["profile_model"] == {"type": "number", "law": "affine"}
    assert len(confirmation["artifact"]["sha256"]) == 64
    # and the *declaration override* names the sweep evidence, not only the artifact
    override = blend["evidence"]["enum_declaration_overridden"]
    assert override["declared_members"] == 257 and override["measured_states"] == 257
    assert override["range_from_the_declaration"] == {"min": 0.0, "max": 2.0}
    assert override["range_disagrees_with_the_declaration"] is False
    assert "continuous between" in override["what_this_does_not_decide"]
    # the numeric model round-trips through the ordinary path, both ways
    assert V.normalized_to_native(blend, 0.0) == 0.0
    assert abs(float(V.normalized_to_native(blend, 0.5)) - 1.0) < 1e-6
    assert abs(float(V.to_normalized(blend, 2.0)) - 1.0) < 1e-6


def test_without_an_artifact_the_profile_is_exactly_what_it_was(tmp_path):
    """The round must not change the offline default, so the pinned-off state is asserted."""
    plain = VitalProvider(registry=PB.load_registry(directory=str(REGISTRY_DIR)))
    profile = plain.build_profile(dict(IDENTITY))
    block = profile["enum_boundaries"]
    assert block["applied"] is False, (
        "the offline suite pins the artifact environment off, so this is the state the "
        "rest of the suite sees"
    )
    assert block["state"] == "refused", "a configured-but-unreadable path is a refusal"
    assert block["refusals"]
    assert "vital_enum_boundaries.json" not in [
        row["file"] for row in profile["provenance"]["sources"]
    ]
    for control in profile["controls"]:
        params = (control.get("law") or {}).get("params") or {}
        assert params.get("boundary_evidence") is None


def test_every_write_target_lands_in_its_own_measured_band(measured_profile):
    """The round-trip invariant, checked over every measured enum in the profile."""
    checked = 0
    for control in measured_profile["controls"]:
        if control.get("type") != "enum":
            continue
        params = control["law"].get("params") or {}
        evidence = params.get("boundary_evidence")
        if not evidence or evidence.get("state") == "no_band_structure":
            continue
        members = [str(member["id"]) for member in control["enum_values"]]
        for index, member in enumerate(members):
            checked += 1
            back = V.to_normalized(control, float(index))
            assert V.normalized_to_native(control, back) == float(index), (
                f"{control['id']}/{member}: the normalized value written for a member must "
                "read back as that member"
            )
    assert checked == 11, (
        f"expected chorus_tempo's 11 members to be checked in both directions, saw {checked}"
    )


def test_the_artifact_bytes_are_key_material(tmp_path):
    provider = fixture_provider()
    rows = provider.profile_source_rows(dict(IDENTITY))
    assert rows[-1]["file"] == "vital_enum_boundaries.json"
    other = tmp_path / "other.json"
    payload = json.loads(ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    payload["controls"]["chorus_on"]["transitions"][0]["hi"] = 0.5
    other.write_text(json.dumps(payload), encoding="utf-8")
    changed = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), enum_boundaries_path=str(other)
    )
    assert changed.profile_source_rows(dict(IDENTITY))[-1]["sha256"] != rows[-1]["sha256"]

    without = VitalProvider(registry=PB.load_registry(directory=str(REGISTRY_DIR)))
    without.enum_boundaries_artifact_path = lambda: None  # type: ignore[assignment]
    assert len(without.profile_source_rows(dict(IDENTITY))) == len(rows) - 1


def test_a_malformed_artifact_is_refused_and_reported_without_touching_geometry(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    profile = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), enum_boundaries_path=str(bad)
    ).build_profile(dict(IDENTITY))
    block = profile["enum_boundaries"]
    assert block["state"] == "refused" and block["applied"] is False
    assert block["refusals"] and "IncompatibleSettings" in block["refusals"][0]
    assert "bad.json" in [row["file"] for row in profile["provenance"]["sources"]]
    for control in profile["controls"]:
        params = (control.get("law") or {}).get("params") or {}
        assert params.get("boundary_evidence") is None


def test_an_artifact_from_another_build_is_not_applied(tmp_path):
    payload = json.loads(ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    payload["identity"] = {**payload["identity"], "param_count": 2858}
    other = tmp_path / "other_build.json"
    other.write_text(json.dumps(payload), encoding="utf-8")
    profile = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), enum_boundaries_path=str(other)
    ).build_profile(dict(IDENTITY))
    block = profile["enum_boundaries"]
    assert block["applied"] is False and block["state"] == "refused"
    assert any("different build" in note for note in block["refusals"])


# ---------------------------------------------------------------------------
# 4. Read-only discipline, and the probe that produces the artifact
# ---------------------------------------------------------------------------


def test_the_module_never_imports_a_host_module_and_takes_no_bridge():
    import inspect

    source = inspect.getsource(EB)
    for banned in ("import reapy", "reascript_api", "ReaperController", "TrackFX_Set"):
        assert banned not in source, f"{banned!r} must not appear in the boundaries module"
    signature = inspect.signature(EB.apply_enum_boundaries)
    assert not any(
        name in ("bridge", "host", "controller") for name in signature.parameters
    ), "the merge must not be able to reach the host"


def test_the_probe_calls_only_read_only_host_functions():
    text = PROBE.read_text(encoding="utf-8")
    assert "TrackFX_FormatParamValueNormalized" in text
    assert "read_only_proof" in text and "chunk_sha256_unchanged" in text
    # the module docstring *names* the setters it does not call, so the check looks
    # at the code: every call site in the probe is an attribute of the reapy API
    code = text.split('"""', 2)[2]
    for banned in ("TrackFX_SetParam", "SetParamNormalized", "TrackFX_SetNamedConfigParm"):
        assert banned not in code, f"the probe must not call {banned}"
    # the budget is real, not decorative: the probe must carry one
    assert "max_calls" in text and "Budget" in text


def test_the_merge_is_bounded_by_the_artifacts_own_size():
    """A record with as many transitions as members cannot make the merge loop."""
    import inspect

    source = inspect.getsource(EB.apply_enum_boundaries)
    assert "for control_id, record in boundaries.controls.items()" in source, (
        "the merge iterates the artifact's controls exactly once, and each control's "
        "transitions once: no recursive or repeated work over the input"
    )


# ---------------------------------------------------------------------------
# 5. The independent review's two reproductions (fixed, and pinned here)
# ---------------------------------------------------------------------------


def test_a_band_narrower_than_a_rounding_step_cannot_escape_its_own_band(tmp_path):
    """Review A3: two transitions two ULP apart make a band 2 ULP wide.

    The write target was computed by rounding the band's midpoint to nine decimals, so
    a band narrower than that rounding produced a target *outside* the band it came
    from - a write that would read back as another member. The invariant is now checked
    for every attached target, on the numbers that are actually attached, and a control
    that cannot satisfy it is refused rather than merged.
    """
    low = 0.40663054591343556
    high = math.nextafter(math.nextafter(low, math.inf), math.inf)
    controls = [control("c", ["A", "B", "C"])]
    records = {"c": measured(["A", "B", "C"], [(0.1, low), (0.2, high)])}
    controls, report, _ = merged(tmp_path, controls, records)
    params = controls[0]["law"]["params"]
    if "band_ends" in (params.get("boundary_evidence") or {}):
        bands = params["boundary_evidence"]["band_ends"]
        targets = params.get("positions") or [
            index / 2.0 for index in range(3)
        ]
        for index, target in enumerate(targets):
            low_edge, high_edge = bands[index], bands[index + 1]
            ok = (
                low_edge <= target <= high_edge
                if (index == 2 and high_edge == 1.0)
                else low_edge <= target < high_edge
            )
            assert ok, (
                f"member {index}'s write target {target!r} escaped its band "
                f"[{low_edge!r}, {high_edge!r})"
            )
        assert V.normalized_to_native(controls[0], targets[1]) == 1.0
    else:
        assert report["controls_refused"] == 1, report["refusals"]
        assert report["refusals"][0]["reason"] == "no_write_target_inside_its_measured_band"


def test_a_write_target_that_must_escape_refuses_the_control(tmp_path, monkeypatch):
    """The same rule, forced: if no target can be placed inside the band, refuse."""
    controls = [control("c", ["A", "B", "C"])]  # uniform positions 0.0 / 0.5 / 1.0
    records = {"c": measured(["A", "B", "C"], [(0.2, 0.2000001), (0.2000001, 0.3)])}
    # the middle band is [0.2000001, 0.3), so 0.5 has to move - and a midpoint that
    # lands outside its own band must make the merge refuse rather than attach a target
    # the read path would resolve to another member
    monkeypatch.setattr(EB, "_midpoint", lambda low, high: 0.9)
    controls, report, _ = merged(tmp_path, controls, records)
    assert report["controls_refused"] == 1
    assert report["refusals"][0]["reason"] == "no_write_target_inside_its_measured_band"
    assert report["refusals"][0]["examples"][0]["member"] == "B"
    assert "switch_points" not in controls[0]["law"]["params"], "geometry must stay untouched"


def test_a_confirmed_within_bracket_must_contain_the_profiles_boundary(tmp_path):
    """Review A4: the state certifies *this* boundary, so a bracket elsewhere is misuse.

    Without the rule the merge would take the far end of a bracket that was never about
    the profile's own boundary and refine to it (the review's input: boundary 0.4,
    bracket [0.1, 0.9] -> switch point 0.9).
    """
    controls = [control("c", ["A", "B"])]  # uniform boundary at 0.5
    record = measured(["A", "B"], [(0.6, 0.9)], state="confirmed_within")
    controls, report, _ = merged(tmp_path, controls, {"c": record})
    assert report["controls_refused"] == 1
    assert report["refusals"][0]["reason"] == "bracket_does_not_contain_the_profile_boundary"
    assert "switch_points" not in controls[0]["law"]["params"]

    # a bracket that *does* contain the boundary is accepted, and, being consistent
    # with the profile's own value, changes nothing
    controls2 = [control("c", ["A", "B"])]
    record2 = measured(["A", "B"], [(0.4999999, 0.5000001)], state="confirmed_within")
    controls2, report2, _ = merged(tmp_path / "second", controls2, {"c": record2})
    assert report2["counts"]["controls_confirmed"] == 1
    assert "switch_points" not in controls2[0]["law"]["params"]


def test_a_band_interior_the_probe_could_not_confirm_refuses_the_control(tmp_path):
    """Review Q1(b)/F3: a spike (or a display whose digits cannot express the band).

    The brackets say where transitions are; they say nothing about what the host prints
    *between* them. A control whose interior check failed is refused rather than merged
    with a map that would misread every value inside that band.
    """
    controls = [control("c", ["A", "B"])]
    record = measured(["A", "B"], [(0.2, 0.20000000000000004)])
    record["band_interiors"] = {
        "checked": 2,
        "verified": 1,
        "mismatches": [{"member": "B", "normalized": 0.6, "band": [0.2, 1.0], "text": "A", "verified": False}],
    }
    controls, report, _ = merged(tmp_path, controls, {"c": record})
    assert report["controls_refused"] == 1
    assert report["refusals"][0]["reason"] == "band_interior_not_verified"
    assert report["refusals"][0]["examples"][0]["text"] == "A"
    assert "switch_points" not in controls[0]["law"]["params"]

    # a fully verified interior check is carried into the evidence
    controls2 = [control("c", ["A", "B"])]
    record2 = measured(["A", "B"], [(0.2, 0.20000000000000004)])
    record2["band_interiors"] = {"checked": 2, "verified": 2, "mismatches": []}
    controls2, _report2, _ = merged(tmp_path / "third", controls2, {"c": record2})
    evidence = controls2[0]["law"]["params"]["boundary_evidence"]
    assert evidence["band_interiors_checked"] == 2
    assert evidence["band_interiors_verified"] == 2
    assert evidence["band_ends"][0] == 0.0


def test_the_validator_rejects_an_attached_target_outside_its_band(measured_profile):
    """Defence in depth: even if a merge were wrong, validation must see it."""
    from reaper_reapy_mcp.plugins.profile import validate_profile

    profile = json.loads(json.dumps(measured_profile))
    target = next(
        control
        for control in profile["controls"]
        if isinstance((control.get("law") or {}).get("params"), dict)
        and (control["law"]["params"].get("boundary_evidence") or {}).get("band_ends")
    )
    params = target["law"]["params"]
    if "positions" not in params:
        params["positions"] = [
            index / max(1, len(target["enum_values"]) - 1)
            for index in range(len(target["enum_values"]))
        ]
    params["positions"][-1] = 1.0
    bands = params["boundary_evidence"]["band_ends"]
    if not (bands[-2] <= 1.0 < bands[-1]):  # force an escaping target
        params["positions"][-1] = bands[-2] - 1e-6
    result = validate_profile(profile)
    assert result.ok is False
    assert any(problem.rule == "enum_write_target_in_band" for problem in result.problems), (
        result.problems
    )


def test_the_validator_rejects_bands_that_disagree_with_the_switch_points(measured_profile):
    from reaper_reapy_mcp.plugins.profile import validate_profile

    profile = json.loads(json.dumps(measured_profile))
    target = next(
        control
        for control in profile["controls"]
        if isinstance((control.get("law") or {}).get("params"), dict)
        and (control["law"]["params"].get("boundary_evidence") or {}).get("band_ends")
    )
    params = target["law"]["params"]
    params["boundary_evidence"]["band_ends"] = [0.5, 0.6, 1.0]
    result = validate_profile(profile)
    assert result.ok is False
    assert any(problem.rule == "enum_band_ends_valid" for problem in result.problems), (
        result.problems
    )


def test_the_probe_verifies_band_interiors_and_writes_byte_identical_output():
    """The probe's own invariants: an interior pass, and a digest of the *file* bytes."""
    text = PROBE.read_text(encoding="utf-8")
    assert "band_interiors" in text, "the probe must verify each band's interior"
    assert 'newline="\\n"' in text, (
        "the artifact must be written without newline translation: its bytes are cache-key "
        "material, and the digest recorded as evidence must be the digest of those bytes"
    )
    assert "sha256_matches_disk" in text, "the probe must verify the digest it reports"
