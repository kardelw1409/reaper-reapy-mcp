"""P22 tests: the host-calibration artifact, its merges, and its refusals.

The fixture `fixtures/vital_calibration/vital_host_calibration.json` is a
self-describing **subset** of a real read-only capture (17-point
`TrackFX_FormatParamValueNormalized` grid + step metadata + proof), adapted only
in its identity block so the offline pipeline can load it. Every `samples` list
in it is the measured host text, so the states the tests pin below are statements
about the real host's formatting, not about a mock:

- a **percent-space** confirmation (`chorus_feedback`: native +/-0.95, host +/-95)
  and a **native-space** one (`chorus_dry_wet`: both 0..100) - the display
  convention is measured per control, never assumed;
- a **declined** comparison with an offset candidate of exactly -60 semitones
  (`filter_1_cutoff`) - the document-vs-display space P19 found by hand,
  reproduced independently by this channel;
- a weaker `consistent_within_step` state (`volume`) that promotes nothing;
- a boundary enum that confirms (`chorus_tempo`, whose grid point 0.25 formats as
  "8/1" - the half-up rounding fix is what makes that agree).

Everything here runs offline; conftest pins the calibration *environment* off, and
each test that wants the artifact passes it explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import cache as C
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import IncompatibleSettings
from reaper_reapy_mcp.plugins.providers.vital import calibration as CAL
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider
from reaper_reapy_mcp.plugins.resolve import resolve_instance_profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"
CALIBRATION_FIXTURE = FIXTURES / "vital_calibration" / "vital_host_calibration.json"

IDENTITY = {
    "fx_name": PB.VITAL_FX_NAME,
    "class_id": PB.VITAL_CLASS_ID,
    "module": PB.VITAL_MODULE,
    "param_count": 8,
}


def fixture_provider(**kwargs) -> VitalProvider:
    return VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        calibration_path=str(CALIBRATION_FIXTURE),
        **kwargs,
    )


@pytest.fixture(scope="module")
def calibrated_profile() -> dict:
    provider = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        calibration_path=str(CALIBRATION_FIXTURE),
    )
    return provider.build_profile(dict(IDENTITY))


def control_map(profile: dict) -> dict[str, dict]:
    return {str(control["id"]): control for control in profile["controls"]}


# ---------------------------------------------------------------------------
# 1. The fixture-driven outcome, pinned per control
# ---------------------------------------------------------------------------


def test_the_calibration_block_names_the_artifact_and_its_proof(calibrated_profile):
    block = calibrated_profile["calibration"]
    assert block["state"] == "applied" and block["applied"] is True
    assert block["artifact"]["file"] == "vital_host_calibration.json"
    assert len(block["artifact"]["sha256"]) == 64
    assert block["method_version"] == CAL.CALIBRATION_METHOD_VERSION
    assert block["grid_points"] == 17
    assert block["proof"]["values_unchanged"] is True
    assert block["proof"]["chunk_sha256_unchanged"] is True
    # the artifact's bytes are a declared source, so they are cache-key material
    sources = [row["file"] for row in calibrated_profile["provenance"]["sources"]]
    assert sources[-1] == "vital_host_calibration.json"
    # the host's own metadata is recorded, including the *absent* default
    assert block["host_param_space"]["uniform_0_1"] is True
    assert block["host_default_value"]["available"] is False
    assert block["host_default_value"]["answered"] == 0


def test_percent_and_native_display_spaces_are_measured_not_assumed(calibrated_profile):
    controls = control_map(calibrated_profile)
    feedback = controls["chorus_feedback"]["evidence"]["confirmation"]
    assert feedback["state"] == "confirmed"
    assert feedback["display_space"] == "percent_space", feedback
    dry_wet = controls["chorus_dry_wet"]["evidence"]["confirmation"]
    assert dry_wet["state"] == "confirmed"
    assert dry_wet["display_space"] == "native_space", dry_wet


def test_a_space_disagreement_is_declined_and_its_offset_recorded_as_a_candidate(
    calibrated_profile,
):
    controls = control_map(calibrated_profile)
    cutoff = controls["filter_1_cutoff"]
    confirmation = cutoff["evidence"]["confirmation"]
    assert confirmation["state"] == "declined"
    candidate = confirmation["display_offset_candidate"]
    assert candidate["state"] == "candidate"
    assert candidate["offset"] == pytest.approx(-60.0, abs=1e-9)
    # a candidate is a record, not a conversion: the law and range are untouched
    assert cutoff["law"]["kind"] == "affine"
    assert cutoff["range"] == {"min": 8.0, "max": 136.0}


def test_the_declined_controls_do_not_lose_their_evidence_strength(calibrated_profile):
    controls = control_map(calibrated_profile)
    for control_id in ("chorus_delay_1", "chorus_delay_2", "lfo_1_frequency", "reverb_decay_time"):
        confirmation = controls[control_id]["evidence"]["confirmation"]
        assert confirmation["state"] == "declined", (control_id, confirmation)
        assert controls[control_id]["evidence"]["strength"] in ("weak", "strong")
        assert "strength_before" not in controls[control_id]["evidence"], (
            "a declined comparison must not promote (or demote) anything"
        )


def test_consistent_within_step_records_the_step_and_promotes_nothing(calibrated_profile):
    controls = control_map(calibrated_profile)
    volume = controls["volume"]
    confirmation = volume["evidence"]["confirmation"]
    assert confirmation["state"] == "consistent_within_step", confirmation
    assert confirmation["step"] > 0
    assert "strength_before" not in volume["evidence"]


def test_a_promoted_confirmation_records_what_it_was_before(calibrated_profile):
    controls = control_map(calibrated_profile)
    promoted = [
        control
        for control in calibrated_profile["controls"]
        if ((control.get("evidence") or {}).get("confirmation") or {}).get("state") == "confirmed"
    ]
    assert promoted, "the fixture must confirm something"
    for control in promoted:
        evidence = control["evidence"]
        assert evidence["strength"] == "strong", control["id"]
        assert isinstance(evidence["strength_before"], str), control["id"]
    assert controls["chorus_tempo"]["evidence"]["strength"] == "strong"


def test_host_step_metadata_is_recorded_with_its_provenance(calibrated_profile):
    controls = control_map(calibrated_profile)
    tempo = controls["chorus_tempo"]["host_steps"]
    assert tempo["step"] == pytest.approx(0.1)
    assert tempo["is_toggle"] is False
    assert tempo["source"]["kind"] == "host_parameter_step_sizes"
    assert tempo["source"]["call"] == "TrackFX_GetParameterStepSizes"
    assert tempo["source"]["artifact"]["file"] == "vital_host_calibration.json"
    assert calibrated_profile["calibration"]["counts"]["steps_recorded"] >= 1


def test_no_default_is_ever_invented(calibrated_profile):
    for control in calibrated_profile["controls"]:
        assert "default" not in control, control["id"]
        assert "default_value" not in control, control["id"]


# ---------------------------------------------------------------------------
# 2. The unit-fill rules on a synthetic artifact
# ---------------------------------------------------------------------------


def synthetic_artifact(tmp_path: Path, controls: dict, *, identity=None) -> Path:
    payload = {
        "kind": CAL.CALIBRATION_KIND,
        "method_version": CAL.CALIBRATION_METHOD_VERSION,
        "generated_utc": "2026-01-01T00:00:00Z",
        "host": {"name": "reaper"},
        "identity": identity or dict(IDENTITY),
        "grid": [0.0, 0.5, 1.0],
        "controls": controls,
        "read_only_proof": {"values_unchanged": True, "chunk_sha256_unchanged": True},
        "host_param_space": {"uniform_0_1": True},
        "host_default_value": {"available": False, "answered": 0},
    }
    path = tmp_path / "calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def entry(samples, *, token=None, numeric=True, steps=None) -> dict:
    return {
        "index": 0,
        "samples": list(samples),
        "numeric": bool(numeric),
        "unit_token": token,
        "descending": False,
        "labels": None,
        "steps": steps,
    }


def build_with(tmp_path: Path, controls: dict, *, identity=None) -> dict:
    path = synthetic_artifact(tmp_path, controls, identity=identity)
    provider = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), calibration_path=str(path)
    )
    return provider.build_profile(dict(IDENTITY))


def test_a_single_stable_token_fills_a_missing_unit_with_provenance(tmp_path):
    profile = build_with(
        tmp_path,
        {"chorus_cutoff": entry(["1 Hz", "2 Hz", "4 Hz"], token="Hz")},
    )
    control = control_map(profile)["chorus_cutoff"]
    assert control["unit"] == "Hz"
    source = control["unit_source"]
    assert source["kind"] == "host_formatted_display"
    assert source["evidence"] == "measured"
    assert source["samples"] == 3


def test_an_existing_unit_is_never_overwritten(tmp_path):
    profile = build_with(
        tmp_path,
        {"beats_per_minute": entry(["20 Hz", "30 Hz", "40 Hz"], token="Hz")},
    )
    control = control_map(profile)["beats_per_minute"]
    # the registry declares no unit for this one, so the fill *does* apply - pick a
    # control whose unit is declared instead
    assert control["unit"] == "Hz"
    profile2 = build_with(
        tmp_path / "second",
        {"volume": entry(["0 dB", "10 dB", "20 dB"], token="dB")},
    )
    volume = control_map(profile2)["volume"]
    assert volume["unit"] == "dB" and "unit_source" not in volume


def test_a_non_numeric_or_short_entry_never_fills_a_unit(tmp_path):
    profile = build_with(
        tmp_path,
        {
            "chorus_cutoff": entry(["Off", "On", "On"], token="Hz", numeric=False),
            "beats_per_minute": entry(["1 Hz", "", ""], token="Hz"),
        },
    )
    controls = control_map(profile)
    assert "unit_source" not in controls["chorus_cutoff"]
    assert "unit_source" not in controls["beats_per_minute"]
    counts = profile["calibration"]["counts"]
    assert counts["units_filled"] == 0 and counts["units_declined"] == 2


def test_an_entry_for_an_unknown_control_is_ignored_entirely(tmp_path):
    profile = build_with(tmp_path, {"no_such_control": entry(["1 Hz", "2 Hz", "3 Hz"], token="Hz")})
    assert profile["calibration"]["applied"] is True
    assert profile["calibration"]["counts"]["units_filled"] == 0


# ---------------------------------------------------------------------------
# 3. Refusals: a malformed or foreign artifact is never half-applied
# ---------------------------------------------------------------------------


def test_a_malformed_artifact_is_refused_and_reported(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    provider = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), calibration_path=str(bad)
    )
    profile = provider.build_profile(dict(IDENTITY))
    block = profile["calibration"]
    assert block["state"] == "refused" and block["applied"] is False
    assert block["refusals"], block
    # the bytes still travel in the sources: a changed (even broken) artifact must
    # invalidate the cache entry
    assert [row["file"] for row in profile["provenance"]["sources"]][-1] == "bad.json"
    assert not any(control.get("unit_source") for control in profile["controls"])


def test_an_unknown_kind_or_method_version_is_refused(tmp_path):
    for payload in (
        {"kind": "something-else", "method_version": 1},
        {"kind": CAL.CALIBRATION_KIND, "method_version": 99},
    ):
        path = tmp_path / "artifact.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(IncompatibleSettings):
            CAL.load_calibration(path)


def test_a_grid_or_sample_count_mismatch_is_refused(tmp_path):
    controls = {"chorus_cutoff": entry(["1 Hz", "2 Hz"], token="Hz")}  # 2 samples, grid 3
    with pytest.raises(IncompatibleSettings) as excinfo:
        CAL.load_calibration(synthetic_artifact(tmp_path, controls))
    assert "samples" in excinfo.value.message


def test_an_artifact_from_another_build_is_refused(tmp_path):
    profile = build_with(
        tmp_path,
        {"chorus_cutoff": entry(["1 Hz", "2 Hz", "4 Hz"], token="Hz")},
        identity={**IDENTITY, "param_count": 2858},
    )
    block = profile["calibration"]
    assert block["state"] == "refused" and block["applied"] is False
    assert any("different build" in note for note in block["refusals"])
    assert "unit_source" not in control_map(profile)["chorus_cutoff"]


def test_an_unreadable_configured_artifact_is_refused_but_an_absent_one_is_merely_absent():
    from pathlib import Path as _Path

    provider = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)),
        calibration_path=str(_Path(FIXTURES) / "does_not_exist.json"),
    )
    profile = provider.build_profile(dict(IDENTITY))
    assert profile["calibration"]["state"] == "refused"
    assert profile["calibration"]["refusals"]

    # the default location holding nothing is the *normal* state
    default_provider = VitalProvider(registry=PB.load_registry(directory=str(REGISTRY_DIR)))
    default_provider.calibration_artifact_path = lambda: None  # type: ignore[assignment]
    absent = default_provider.build_profile(dict(IDENTITY))
    assert absent["calibration"]["state"] == "absent"
    assert absent["calibration"]["refusals"] == []


# ---------------------------------------------------------------------------
# 4. Key material: the artifact's bytes are load-bearing
# ---------------------------------------------------------------------------


def test_the_artifact_bytes_change_the_cache_key_and_the_profile(tmp_path):
    provider = fixture_provider()
    baseline = C.build_capability_profile(provider, dict(IDENTITY))
    key_a = baseline.key_for(str(provider.id), dict(IDENTITY))

    other = tmp_path / "other_calibration.json"
    payload = json.loads(CALIBRATION_FIXTURE.read_text(encoding="utf-8"))
    payload["controls"]["chorus_cutoff"]["unit_token"] = None
    payload["controls"]["chorus_cutoff"]["samples"] = ["x", "y", "z"]
    payload["controls"]["chorus_cutoff"]["numeric"] = False
    other.write_text(json.dumps(payload), encoding="utf-8")
    changed = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), calibration_path=str(other)
    )
    build_b = C.build_capability_profile(changed, dict(IDENTITY))
    assert build_b.key_for(str(changed.id), dict(IDENTITY)) != key_a, (
        "a changed calibration artifact must move the cache key (its bytes are a source)"
    )


def test_removing_the_artifact_from_the_key_material_changes_the_key(tmp_path):
    with_artifact = fixture_provider()
    rows_with = with_artifact.profile_source_rows(dict(IDENTITY))
    without = VitalProvider(registry=PB.load_registry(directory=str(REGISTRY_DIR)))
    without.calibration_artifact_path = lambda: None  # type: ignore[assignment]
    rows_without = without.profile_source_rows(dict(IDENTITY))
    assert len(rows_with) == len(rows_without) + 1
    digest_with = C.sources_digest(rows_with)
    digest_without = C.sources_digest(rows_without)
    assert digest_with != digest_without


def test_the_source_rows_the_fast_path_hashes_equal_what_the_build_declares():
    provider = fixture_provider()
    build = C.build_capability_profile(provider, dict(IDENTITY))
    declared = [
        {"file": row["file"], "sha256": row["sha256"]}
        for row in build.profile["provenance"]["sources"]
    ]
    assert provider.profile_source_rows(dict(IDENTITY)) == declared


# ---------------------------------------------------------------------------
# 5. The warm path with a calibration-derived profile
# ---------------------------------------------------------------------------


class CountingVitalProvider(VitalProvider):
    builds = 0

    def build_profile(self, identity):  # type: ignore[override]
        type(self).builds += 1
        return super().build_profile(identity)


def test_a_calibrated_profile_is_served_warm_without_a_rebuild(tmp_path):
    slot = MH.MockFxSlot(
        name=IDENTITY["fx_name"], module=IDENTITY["module"], class_id=IDENTITY["class_id"]
    )
    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [slot])])
    registry = PB.load_registry(directory=str(REGISTRY_DIR))
    cold = CountingVitalProvider(registry=registry, calibration_path=str(CALIBRATION_FIXTURE))
    CountingVitalProvider.builds = 0
    cache = C.ProfileCache(provider_id="vital", directory=tmp_path / "cache")
    first = resolve_instance_profile(host, cold, cache=cache)
    assert first.cache.source == "built"
    assert CountingVitalProvider.builds == 1

    warm = CountingVitalProvider(registry=registry, calibration_path=str(CALIBRATION_FIXTURE))
    CountingVitalProvider.builds = 0
    second = resolve_instance_profile(
        host, warm, cache=C.ProfileCache(provider_id="vital", directory=tmp_path / "cache")
    )
    assert second.cache.source == "cache"
    assert CountingVitalProvider.builds == 0, "a warm hit must not rebuild the calibrated profile"
    assert C.profile_content_hash(second.profile) == C.profile_content_hash(first.profile)


# ---------------------------------------------------------------------------
# 6. Read-only discipline and the boundary rounding fix
# ---------------------------------------------------------------------------


def test_the_calibration_module_never_imports_a_host_module():
    import inspect

    source = inspect.getsource(CAL)
    for banned in ("import reapy", "reascript_api", "ReaperController"):
        assert banned not in source, f"{banned!r} must not appear in the calibration module"


def test_a_value_exactly_on_an_enum_boundary_reads_the_member_that_starts_there():
    two = {
        "id": "sw",
        "type": "enum",
        "enum_values": [{"id": "Off", "label": "Off"}, {"id": "On", "label": "On"}],
        "law": {"kind": "enum_index", "params": {}},
    }
    assert V.normalized_to_native(two, 0.5) == 1.0, (
        "the host formats normalized 0.5 as \"On\" (measured: chorus_on) - half-up "
        "rounding is what makes the read agree"
    )
    assert V.normalized_to_native(two, 0.499999) == 0.0
    eleven = {
        "id": "tempo",
        "type": "enum",
        "enum_values": [{"id": f"m{i}", "label": f"m{i}"} for i in range(11)],
        "law": {"kind": "enum_index", "params": {}},
    }
    # 0.25 * 10 = 2.5 sits exactly on a boundary: the member *starting* there wins
    assert V.normalized_to_native(eleven, 0.25) == 3.0
    # a measured band map still takes precedence over the uniform arithmetic
    measured = {
        "id": "style",
        "type": "enum",
        "enum_values": [{"id": a, "label": a} for a in ("12dB", "24dB", "Notch Blend")],
        "law": {
            "kind": "enum_index",
            "params": {"positions": [0.0, 0.125, 0.375], "switch_points": [0.0, 0.0625, 0.1875]},
        },
    }
    assert V.normalized_to_native(measured, 0.0625) == 1.0
    assert V.normalized_to_native(measured, 0.0) == 0.0


def test_the_live_probe_exists_and_states_its_read_only_discipline():
    probe = Path(__file__).resolve().parent / "live" / "probe_p22_host_calibration.py"
    text = probe.read_text(encoding="utf-8")
    assert "read_only_proof" in text
    assert "values_unchanged" in text and "chunk_sha256_unchanged" in text
    assert "FormatParamValueNormalized" in text


# ---------------------------------------------------------------------------
# 7. Trust crossings and hostile artifacts (P22 review Q2 "WEAK", Q6 "DEFECT")
#
# The artifact is a *source*, and a source is data: what it *claims* about itself
# is verified against what it *contains* before any of it reaches a profile, and
# a malformed claim is a typed refusal rather than an untyped crash.
# ---------------------------------------------------------------------------


def declined_unit_reasons(profile: dict) -> dict[str, str]:
    return {
        row["control"]: row["reason"]
        for row in profile["calibration"]["units_declined"]
        if isinstance(row, dict)
    }


def test_a_unit_claim_that_contradicts_its_own_samples_is_declined(tmp_path):
    """Review counterexample C2: samples say dB, the claim says Hz.

    Before the fix the merge filled `unit="Hz"` with `evidence: "measured"` and a
    note asserting "one stable unit token across every non-empty grid sample" -
    a provenance claim the artifact's own text contradicted, and one that changes
    the `%`-trap's behaviour downstream.
    """
    profile = build_with(
        tmp_path,
        {"chorus_cutoff": entry(["-6.0 dB", "-3.0 dB", "0.0 dB"], token="Hz")},
    )
    control = control_map(profile)["chorus_cutoff"]
    assert "unit_source" not in control
    assert control.get("unit") in (None, "")
    assert declined_unit_reasons(profile)["chorus_cutoff"] == "claim_disagrees_with_samples"
    assert profile["calibration"]["counts"]["units_filled"] == 0


def test_a_numeric_claim_over_label_samples_fills_nothing(tmp_path):
    """Review counterexample C1: `numeric: true` over words, with a token attached."""
    profile = build_with(
        tmp_path,
        {"chorus_cutoff": entry(["hot", "cold", "wet"], token="Hz", numeric=True)},
    )
    control = control_map(profile)["chorus_cutoff"]
    assert "unit_source" not in control
    assert declined_unit_reasons(profile)["chorus_cutoff"] == "claim_disagrees_with_samples"


def test_an_entry_with_no_unit_claim_fills_nothing_and_declines_nothing(tmp_path):
    """No claim means nothing to verify: the absence of a unit is reported as such.

    (A non-numeric display cannot produce a token at all: `_unit_token_from_samples`
    returns `None` for it, so the only shape that could claim a token *and* be
    non-numeric is the dishonest one pinned above.)
    """
    profile = build_with(
        tmp_path,
        {"chorus_cutoff": entry(["Off", "On", "On"], token=None, numeric=False)},
    )
    assert "unit_source" not in control_map(profile)["chorus_cutoff"]
    assert declined_unit_reasons(profile) == {}
    assert profile["calibration"]["counts"]["units_filled"] == 0


def test_too_few_usable_samples_is_recorded_with_its_reason(tmp_path):
    profile = build_with(
        tmp_path,
        {"chorus_cutoff": entry(["1 Hz", "", ""], token="Hz")},
    )
    assert declined_unit_reasons(profile)["chorus_cutoff"] == "too_few_samples"
    assert "unit_source" not in control_map(profile)["chorus_cutoff"]


@pytest.mark.parametrize(
    "steps",
    [
        {"step": "quick", "small": 0.0, "large": 0.0, "is_toggle": False, "reported": True},
        {"step": float("nan"), "small": 0.0, "large": 0.0, "is_toggle": False, "reported": True},
        {"step": float("inf"), "small": 0.0, "large": 0.0, "is_toggle": False, "reported": True},
        {"step": [1], "small": 0.0, "large": 0.0, "is_toggle": False, "reported": True},
    ],
)
def test_a_non_numeric_or_non_finite_step_is_a_typed_refusal(tmp_path, steps):
    """Review defect B: `float(steps.get("step") or 0.0)` raised a raw ValueError.

    `IncompatibleSettings` is what keeps the promise in this module's docstring -
    "every failure is a typed refusal, never half-applied" - and it is what
    `provider.build_profile` catches to report `refused` instead of dying.
    """
    controls = {"chorus_cutoff": entry(["1 Hz", "2 Hz", "4 Hz"], token="Hz", steps=steps)}
    path = synthetic_artifact(tmp_path, controls)
    with pytest.raises(IncompatibleSettings) as excinfo:
        CAL.load_calibration(path)
    assert "step" in excinfo.value.message

    profile = build_with(tmp_path / "as_profile", controls)
    assert profile["calibration"]["state"] == "refused"
    assert not any(control.get("unit_source") for control in profile["controls"])


def test_an_unreadable_sample_magnitude_cannot_overflow_the_confirmation(tmp_path):
    """Review defect A: a crafted ``"1e999999"`` sample reached ``0.5*10.0**n``.

    Two things had to be true at once: the power must not overflow, and the sample
    must not be compared as ``inf`` (which would write a non-standard ``Infinity``
    into the profile's JSON).
    """
    profile = build_with(
        tmp_path,
        {
            "chorus_cutoff": entry(["1e999999", "1e999999", "1e999999"], token=None),
            "chorus_feedback": entry(["1e999999", "2e999999", "3e999999"], token=None),
        },
    )
    controls = control_map(profile)
    for control_id in ("chorus_cutoff", "chorus_feedback"):
        confirmation = controls[control_id]["evidence"]["confirmation"]
        # every sample was unreadable, so there is nothing to compare - and the
        # unreadable sample is skipped, never treated as a huge tolerance
        assert confirmation["state"] in ("insufficient_points", "declined"), confirmation
    text = json.dumps(profile)
    for token in ("Infinity", "-Infinity", "NaN"):
        assert token not in text, f"{token} must not reach the profile JSON"


@pytest.mark.parametrize(
    "grid",
    [
        [0.0, 0.5, 1.5],
        [0.0, -0.25, 1.0],
        [0.0, float("nan"), 1.0],
        [0.0, float("inf"), 1.0],
        [0.0, 0.75, 0.25],
        [0.0, "half", 1.0],
    ],
)
def test_a_grid_that_is_not_a_normalized_sweep_is_refused(tmp_path, grid):
    """Review defect D: `[0.0, 1e300, -5.0, NaN, 2.0]` was accepted verbatim."""
    payload = {
        "kind": CAL.CALIBRATION_KIND,
        "method_version": CAL.CALIBRATION_METHOD_VERSION,
        "identity": dict(IDENTITY),
        "grid": grid,
        "controls": {"chorus_cutoff": entry(["1 Hz", "2 Hz", "4 Hz"])},
    }
    path = tmp_path / "grid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        CAL.load_calibration(path)
    assert "grid" in excinfo.value.message


def test_an_oversized_artifact_sample_or_grid_is_refused(tmp_path, monkeypatch):
    """Review defect: bytes, samples and grid points were unbounded."""
    controls = {"chorus_cutoff": entry(["1 Hz", "2 Hz", "4 Hz"], token="Hz")}
    path = synthetic_artifact(tmp_path, controls)
    monkeypatch.setattr(CAL, "_MAX_ARTIFACT_BYTES", 16)
    with pytest.raises(IncompatibleSettings) as excinfo:
        CAL.load_calibration(path)
    assert "beyond the" in excinfo.value.message
    monkeypatch.undo()

    long_sample = tmp_path / "long.json"
    payload = json.loads(synthetic_artifact(tmp_path / "src", controls).read_text(encoding="utf-8"))
    payload["controls"]["chorus_cutoff"]["samples"] = ["1 Hz", "x" * 5000, "4 Hz"]
    long_sample.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        CAL.load_calibration(long_sample)
    assert "longer than" in excinfo.value.message

    many = tmp_path / "many.json"
    payload["grid"] = [i / 5000.0 for i in range(5000)]
    payload["controls"]["chorus_cutoff"]["samples"] = ["1 Hz"] * 5000
    many.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        CAL.load_calibration(many)
    assert "grid has" in excinfo.value.message


def test_the_profile_keeps_only_scalar_calibration_fields(tmp_path, monkeypatch):
    """A 500 MB proof blob must not become 500 MB of validating profile content."""
    path = synthetic_artifact(tmp_path, {"chorus_cutoff": entry(["1 Hz", "2 Hz", "4 Hz"])})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["read_only_proof"] = {
        "values_unchanged": True,
        "chunk_sha256_unchanged": True,
        "deep": {"blob": "x" * 10000},
    }
    payload["host_param_space"] = {"uniform_0_1": True, "deep": {"blob": "y" * 10000}}
    payload["host_default_value"] = {"available": False, "deep": {"blob": "z" * 10000}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    provider = VitalProvider(
        registry=PB.load_registry(directory=str(REGISTRY_DIR)), calibration_path=str(path)
    )
    profile = provider.build_profile(dict(IDENTITY))
    block = profile["calibration"]
    assert block["proof"]["values_unchanged"] is True
    assert block["proof"]["chunk_sha256_unchanged"] is True
    assert block["host_param_space"]["uniform_0_1"] is True
    assert block["host_default_value"]["available"] is False
    for name in ("proof", "host_param_space", "host_default_value"):
        assert all(
            isinstance(value, (bool, int, float, str)) for value in block[name].values()
        ), (name, block[name])
    text = json.dumps(profile)
    assert "x" * 1000 not in text and "y" * 1000 not in text and "z" * 1000 not in text


def test_the_validator_refuses_a_non_finite_number_in_the_host_metadata(calibrated_profile):
    """Review defect E: `json` parses bare `NaN`, and `_is_number` accepted it."""
    from reaper_reapy_mcp.plugins.profile import validate_profile

    profile = json.loads(json.dumps(calibrated_profile))
    target = next(
        control for control in profile["controls"] if isinstance(control.get("host_steps"), dict)
    )
    target["host_steps"]["step"] = float("nan")
    result = validate_profile(profile)
    assert result.ok is False
    assert any(problem.rule == "host_steps_valid" for problem in result.problems), result.problems
