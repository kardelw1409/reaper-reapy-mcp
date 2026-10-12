"""P24 tests, workstream A part 2: the three states that have **no name**.

P23 measured that `filter_1_style`, `filter_2_style` and `filter_fx_style` each have
a region where the host prints **nothing**: every transition below 0.5625 names a
member ("12dB", "24dB", "Notch Blend", "Notch Spread", "B/P/N"), and above it the
display is empty. The question this round had to answer is whether a name exists
that the profile is missing, and the honest answer came from the plugin's own
source rather than from a guess:

* ``src/interface/look_and_feel/synth_strings.h`` defines
  ``kFilterStyleNames`` with **five** entries - exactly the five the sweep
  measured, in that order;
* ``src/common/synth_parameters.cpp`` declares the ``style`` parameter over
  ``0.0 .. 9.0`` as ``ValueDetails::kIndexed`` bound to that array;
* ``src/interface/editor_sections/filter_section.cpp`` selects the displayed name
  from a **model-dependent** table (``kFilterStyleNames`` for Analog/Dirty/Ladder/
  Digital, ``kDiodeStyleNames`` for Diode, and so on).

So the region has no label *to* print, and the round must not invent one - the
profile records the measured region, the reason, and where the reason comes from.
The same source also says the labels depend on `filter_N_model`; that part is **not**
verified here (changing the model needs a write, and no round since P5 writes), so it
is reported as a source-based implication, not as a measured fact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ARTIFACT_FIXTURE = FIXTURES / "vital_enum_boundaries" / "vital_enum_boundaries.json"
CALIBRATION = Path.home() / "AppData" / "Local" / "reaper_reapy_mcp" / "calibration"
STYLES = ("filter_1_style", "filter_2_style", "filter_fx_style")
NAMED = ["12dB", "24dB", "Notch Blend", "Notch Spread", "B/P/N"]


@pytest.fixture(scope="module")
def measured_profile() -> dict:
    """The real registry with the live P23 artifact - skipped when either is absent."""
    import os

    artifact = CALIBRATION / "vital_enum_boundaries.json"
    try:
        PB.load_registry(directory=PB.DEFAULT_REGISTRY_DIR)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"the measured registry is not present: {exc}")
    if not artifact.exists():
        pytest.skip("the measured enum-boundary artifact is not present")
    saved = {
        name: os.environ.get(name)
        for name in ("REAPER_MCP_VITAL_ENUM_BOUNDARIES", "REAPER_MCP_VITAL_CALIBRATION")
    }
    os.environ["REAPER_MCP_VITAL_ENUM_BOUNDARIES"] = str(artifact)
    calibration = CALIBRATION / "vital_host_calibration.json"
    if calibration.exists():
        os.environ["REAPER_MCP_VITAL_CALIBRATION"] = str(calibration)
    try:
        yield VitalProvider().build_profile(
            {
                "fx_name": PB.VITAL_FX_NAME,
                "class_id": PB.VITAL_CLASS_ID,
                "module": PB.VITAL_MODULE,
                "param_count": 2858,
            }
        )
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def by_id(profile: dict, control_id: str) -> dict:
    for control in profile["controls"]:
        if control["id"] == control_id:
            return control
    raise AssertionError(f"control {control_id!r} is not in the profile")


def test_the_three_styles_keep_five_members_and_no_invented_sixth(measured_profile):
    for control_id in STYLES:
        control = by_id(measured_profile, control_id)
        assert control["type"] == "enum", control_id
        members = [str(member["id"]) for member in control["enum_values"]]
        assert members == NAMED, (control_id, members)
        assert len(members) == 5, "a sixth name would have to come from somewhere"


def test_the_unnamed_region_is_recorded_with_its_reason_and_the_source(measured_profile):
    regions = {
        row["control"]: row for row in measured_profile["enum_boundaries"]["unlabelled_regions"]
    }
    assert sorted(regions) == list(STYLES), sorted(regions)
    for control_id, region in regions.items():
        assert region["text"] == "", control_id
        assert region["lo"] == pytest.approx(0.56249997, abs=1e-8), control_id
        assert region["hi"] == pytest.approx(0.56249997, abs=1e-8), control_id
        assert "no name to print" in region["why"], control_id
        # the explanation names the plugin's own files, so a reader can check it
        assert "kFilterStyleNames" in region["source"], control_id
        assert "synth_parameters.cpp" in region["source"], control_id
        assert "filter_section.cpp" in region["source"], control_id


def test_the_last_named_member_owns_only_the_region_below_the_tail(measured_profile):
    """A write above the tail would select a member the host never prints there."""
    for control_id in STYLES:
        control = by_id(measured_profile, control_id)
        params = control["law"]["params"]
        bands = params["boundary_evidence"]["band_ends"]
        assert bands[-1] == pytest.approx(0.56249997, abs=1e-8), control_id
        assert params["positions"][-1] < bands[-1], control_id
        # inside the last named band the read agrees with the member the host prints
        assert V.normalized_to_native(control, 0.5) == 4.0, control_id
        # and above the tail the host prints nothing: the profile maps it to the last
        # member, which is the *declared* behaviour rather than a name it invented
        assert V.to_normalized(control, 4.0) == pytest.approx(
            params["positions"][-1], abs=1e-12
        ), control_id


def test_the_source_claim_is_a_quotation_not_a_paraphrase(measured_profile):
    """The reason cites files; this pins that it does so with the array's own name."""
    region = measured_profile["enum_boundaries"]["unlabelled_regions"][0]
    blob = json.dumps(region, ensure_ascii=False)
    assert "kFilterStyleNames" in blob
    assert "model-dependent" in blob, "the model dependence must be stated as an implication"
