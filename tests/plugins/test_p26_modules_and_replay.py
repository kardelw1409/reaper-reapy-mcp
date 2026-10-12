"""P26 module inference and offline replay (brief §17 scenarios 12-20).

Scenario map for this file:

- **12** repeated modules with a shared structure;
- **13** identical names in different modules;
- **14** uninformative names and unclear semantics (``unknown`` is a legal answer);
- **15** missing or invalid discovery artifacts (the replay refuses and says why);
- **17** generic discovery without provider metadata - proved by replaying a
  fixture whose provider-authored *claims* were stripped: the result must be
  identical, because those fields are not decision inputs;
- **18** a provider overlay changes the comparison, not the inference;
- **20** the engine keeps its place in the layering (no profile/cache/provider
  import), so it cannot be reached by the profile path by accident.

The replay tests use the pinned fixtures in `tests/plugins/fixtures`, so they are
hermetic; a second group reads the machine's own recorded artifacts when they
exist and asserts only structural invariants, because their content belongs to a
live session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins.discovery import replay as RP
from reaper_reapy_mcp.plugins.discovery.modules import infer_modules, tokenize, family_key_of
from reaper_reapy_mcp.plugins.discovery.pipeline import EngineConfig
from reaper_reapy_mcp.plugins.discovery import report as REPORT

import discovery_double as D

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CALIBRATION_FIXTURE = FIXTURES / "vital_calibration" / "vital_host_calibration.json"
BOUNDARY_FIXTURE = FIXTURES / "vital_enum_boundaries" / "vital_enum_boundaries.json"


def rows(names: list[str], *, start: int = 0, section: str | None = None, unit: str | None = None) -> list[dict]:
    return [
        {"index": start + offset, "name": name, "section": section, "unit": unit}
        for offset, name in enumerate(names)
    ]


def node_ids(result) -> dict[str, list[int]]:
    return {node.id: node.controls for node in result.nodes if node.parent is None}


# ---------------------------------------------------------------------------
# 12-14: module inference
# ---------------------------------------------------------------------------


def test_scenario_12_repeated_modules_of_the_same_shape_are_one_module_with_a_recorded_split():
    """A repeated token shape is evidence of a shared structure, not of a single
    module: the merge is published as *inferred* and the per-family split travels
    with it, so a reader that wants three modules has them."""
    names = [f"group_{group}_{token}" for group in (1, 2, 3) for token in ("alpha", "beta", "gamma", "delta")]
    result = infer_modules(rows(names))
    top = node_ids(result)
    assert list(top) == ["group"]
    assert len(top["group"]) == 12
    node = next(item for item in result.nodes if item.id == "group")
    assert node.status == "inferred"
    splits = [row for row in node.alternatives if row.get("split")]
    assert sorted(row["split"] for row in splits) == ["group 1", "group 2", "group 3"]
    assert all(len(row["controls"]) == 4 for row in splits)
    assert "prefix" in result.signals_used and "repetition" in result.signals_used


def test_numbered_families_of_different_sizes_stay_separate_modules():
    """The oscillator case: three numbered families, each big enough to be a module
    of its own, must not be merged into one."""
    names = [
        f"block_{block}_{token}"
        for block in (1, 2, 3)
        for token in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu", "nu", "xi")
    ]
    result = infer_modules(rows(names))
    top = node_ids(result)
    assert sorted(top) == ["block 1", "block 2", "block 3"]
    assert all(len(controls) == 14 for controls in top.values())


def test_scenario_12b_many_small_repeated_families_become_one_module_with_members():
    names = [
        f"matrix_{slot}_{token}"
        for slot in range(1, 6)
        for token in ("source", "amount", "depth")
    ]
    result = infer_modules(rows(names))
    top = node_ids(result)
    assert list(top) == ["matrix"]
    assert len(top["matrix"]) == 15
    assert "repetition" in result.signals_used
    parent = next(node for node in result.nodes if node.id == "matrix")
    assert parent.status == "inferred"
    children = [node for node in result.nodes if node.parent == "matrix"]
    assert children, "a repeated structure must produce member submodules"
    assert all(child.controls for child in children)
    assert sum(len(child.controls) for child in children) == 15


def test_scenario_13_the_same_name_in_two_families_lands_in_two_modules():
    names = ["filter_1_cutoff", "filter_1_resonance", "filter_2_cutoff", "filter_2_resonance"]
    result = infer_modules(rows(names))
    top = node_ids(result)
    assert sorted(top) == ["filter 1", "filter 2"]
    assert 0 in top["filter 1"] and 2 in top["filter 2"]
    assert top["filter 1"] != top["filter 2"]
    # and a shared token (`cutoff`) did not merge them
    assert len(result.nodes) == 2


def test_scenario_14_uninformative_names_are_one_declared_unknown_module():
    names = [f"param_{index}" for index in range(1, 21)]
    result = infer_modules(rows(names))
    assert [node.id for node in result.nodes] == ["unlabelled"]
    node = result.nodes[0]
    assert node.status == "unknown" and node.confidence == "none"
    assert len(node.controls) == 20
    assert "no informative name" in node.reason


def test_flat_uninformative_list_does_not_invent_a_structure():
    names = ["knob", "value", "control", "x", "y"]
    result = infer_modules(rows(names))
    assert [node.id for node in result.nodes] == ["unlabelled"]
    assert result.nodes[0].status == "unknown"


def test_grouping_does_not_require_contiguous_indices():
    names = ["block_a_one", "block_a_two", "block_b_one", "block_a_three"]
    result = infer_modules(rows(names, start=10))
    top = node_ids(result)
    assert sorted(top) == ["block a", "block b"]
    assert top["block a"] == [10, 11, 13]


def test_a_short_property_word_does_not_split_a_module():
    """Found by the P26 independent review: `Delay L Time`, `Delay R Time`,
    `Delay Mix` is ONE effect. A three-character word such as `mix` is a property,
    so only numbers and one/two-character tokens may promote a prefix."""
    names = ["delay_l_time", "delay_l_feedback", "delay_r_time", "delay_r_feedback", "delay_mix"]
    result = infer_modules(rows(names))
    assert list(node_ids(result)) == ["delay"]
    assert len(node_ids(result)["delay"]) == 5


def test_a_long_second_token_is_a_property_not_a_family_discriminator():
    """`section_gain` and `section_mix` are two properties of one section - the
    second pass must not split a family on a long word."""
    names = ["section_gain", "section_mix", "section_level"]
    result = infer_modules(rows(names))
    assert list(node_ids(result)) == ["section"]


def test_a_repeated_token_creates_submodules_but_a_one_off_token_does_not():
    names = [
        "unit_1_pitch_coarse",
        "unit_1_pitch_fine",
        "unit_1_volume_out",
        "unit_1_volume_in",
        "unit_1_oddball",
        "unit_1_extra",
    ]
    result = infer_modules(rows(names))
    children = {node.id for node in result.nodes if node.parent}
    assert children == {"unit 1 / pitch", "unit 1 / volume"}


def test_a_declared_section_outranks_the_name_inference():
    names = ["alpha_one", "alpha_two", "alpha_three"]
    result = infer_modules(rows(names, section="Delay Section"))
    node = result.nodes[0]
    assert node.label == "Delay Section"
    assert node.status == "confirmed" and node.confidence == "strong"
    assert node.signals[0] == "section"


def test_a_vocabulary_match_alone_is_only_a_candidate():
    names = ["thing_one", "thing_two"]
    result = infer_modules(rows(names))
    assert result.nodes[0].status == "candidate"
    assert result.nodes[0].kind == "unknown"


def test_a_family_whose_tokens_are_ambiguous_records_its_alternatives():
    names = ["delay_amount_one", "delay_amount_two"]
    result = infer_modules(rows(names))
    node = result.nodes[0]
    assert node.kind in ("effects", "envelope", "modulation")
    assert node.alternatives


def test_two_families_are_not_a_repetition():
    names = ["slot_1_a", "slot_1_b", "slot_2_a", "slot_2_b"]
    result = infer_modules(rows(names))
    assert sorted(node_ids(result)) == ["slot 1", "slot 2"]
    parent = next(node for node in result.nodes if node.id == "slot 1")
    assert parent.controls and len(parent.controls) == 2


def test_grouping_rows_are_all_placed_or_reported():
    names = ["a_one", "a_two", "param_3", "b_one"]
    result = infer_modules(rows(names))
    placed = {index for node in result.nodes if not node.parent for index in node.controls}
    assert placed == {0, 1, 2, 3}
    assert result.notes == []


def test_tokenizer_splits_digits_from_stems():
    assert tokenize("oscCutoff12") == ["osc", "cutoff", "12"]
    assert tokenize("MIDI CC 1|1") == ["midi", "cc", "1", "1"]
    assert tokenize(None, ident="0:48") == ["0", "48"]
    assert family_key_of(["group", "2", "level"]) == ("group 2", ("group", "2"))


# ---------------------------------------------------------------------------
# The engine's own placement (not a third-party property)
# ---------------------------------------------------------------------------


def test_positions_are_stable_across_differently_formatted_inputs():
    rows_a = rows(["first_one", "first_two", "second_one"])
    rows_b = rows(["First One", "First Two", "Second One"])
    assert node_ids(infer_modules(rows_a)) == node_ids(infer_modules(rows_b))


# ---------------------------------------------------------------------------
# 15, 17-18: replay and comparison
# ---------------------------------------------------------------------------


def test_scenario_15_an_unknown_document_is_reported_rather_than_half_read(tmp_path):
    path = tmp_path / "mystery.json"
    path.write_text(json.dumps({"kind": "who-knows", "stuff": [1, 2, 3]}), encoding="utf-8")
    source = RP.read_recorded(path)
    assert source.shape == "unknown"
    assert source.records == [] and source.observations == []
    assert source.warnings and "supported shapes" in source.warnings[0]


def test_the_declared_kind_is_data_not_a_dispatch_key(tmp_path):
    """Two documents with the same structure and *different* kind strings must read
    identically: Core cannot carry a list of plugin names to know how to read one."""
    body = {
        "identity": {"fx_name": "X", "class_id": "c", "module": "m", "param_count": 2},
        "grid": [0.0, 0.5, 1.0],
        "controls": {
            "a": {"index": 0, "samples": ["0", "1", "2"], "numeric": True, "unit_token": None},
            "b": {"index": 1, "samples": ["x", "y", "z"], "numeric": False, "unit_token": None},
        },
    }
    paths = []
    for kind in ("some-plugin-recording", "another-plugin-recording"):
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps({**body, "kind": kind}), encoding="utf-8")
        paths.append(path)
    sources = [RP.read_recorded(path) for path in paths]
    assert {source.shape for source in sources} == {"display-samples"}
    assert sources[0].kind != sources[1].kind
    assert [len(source.records) for source in sources] == [2, 2]
    assert [
        [sample.text for sample in entry.samples] for entry in sources[0].observations
    ] == [[sample.text for sample in entry.samples] for entry in sources[1].observations]


def test_a_replay_of_the_pinned_fixture_is_reproducible_and_states_its_origin():
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    assert source.shape == "display-samples" and source.records
    first = RP.replay_from_recorded(source, config=EngineConfig(mode="prepare", grid_points=17))
    second = RP.replay_from_recorded(source, config=EngineConfig(mode="prepare", grid_points=17))
    assert first.to_dict(with_observations=False)["controls"] == second.to_dict(with_observations=False)["controls"]
    assert all(entry.origin == "recorded" for entry in first.observations)
    assert any("recorded observations" in str(row.get("message")) for row in first.warnings)
    assert first.cost.total_calls == 0
    assert first.counts["controls_analyzed"] == len(source.records)


def test_scenario_17_the_provider_authored_claims_in_a_recording_are_not_decision_inputs(tmp_path):
    """The recorded artifact carries fields a *provider* derived (a unit token, a
    member-label list, a numeric flag). Stripping them must not change a single
    verdict: the generic engine reads the samples, and those fields are recorded
    provenance, not evidence it is allowed to use."""
    original = json.loads(CALIBRATION_FIXTURE.read_text(encoding="utf-8"))
    stripped = json.loads(json.dumps(original))
    for row in stripped["controls"].values():
        row.pop("unit_token", None)
        row.pop("labels", None)
        row.pop("numeric", None)
        row.pop("descending", None)
    path = tmp_path / "claims-stripped.json"
    path.write_text(json.dumps(stripped), encoding="utf-8")

    first = RP.replay_from_recorded(RP.read_recorded(CALIBRATION_FIXTURE))
    second = RP.replay_from_recorded(RP.read_recorded(path))
    assert first.to_dict(with_observations=False)["controls"] == second.to_dict(
        with_observations=False
    )["controls"]


def test_the_engine_declares_what_it_can_never_be_told_to_assume():
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    inventory = RP.inventory_from_source(source)
    assert inventory.origin == "recorded"
    assert inventory.attempts["param_metadata"].calls == 0
    assert any("no plugin registry" in note for note in inventory.notes)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _reference_profile(rows: list[dict]) -> dict:
    return {"controls": rows}


def test_comparison_counts_agreement_disagreement_and_withholding_separately():
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(source)
    controls = [
        control
        for control in result.controls
        if control.classification.kind not in ("unknown",)
    ][:6]
    profile_rows = []
    for position, control in enumerate(controls):
        kind = control.classification.kind
        reference_type = "enum" if kind in ("enum", "boolean", "integer_like") else "number"
        profile_rows.append(
            {
                "id": control.name,
                "binding": {"index": control.index},
                "type": reference_type,
                "law": {"kind": "affine"},
                "module": control.module,
                "unit": control.unit,
                "enum_values": [{"id": str(index)} for index in range(2)] if reference_type == "enum" else [],
                "range": {"min": 0.0, "max": 1.0},
            }
        )
    # a deliberately wrong row and a row for a control the replay never saw
    profile_rows.append(
        {
            "id": "made-up",
            "binding": {"index": controls[0].index},
            "type": "number" if profile_rows[0]["type"] == "enum" else "enum",
            "law": {"kind": "power"},
            "module": "Elsewhere",
            "unit": None,
            "enum_values": [{"id": "a"}, {"id": "b"}],
            "range": {"min": -1.0, "max": 1.0},
        }
    )
    comparison = RP.compare_with_profile(result, _reference_profile(profile_rows))
    aggregates = comparison["aggregates"]
    assert aggregates["controls"] == len(profile_rows)
    assert aggregates["true_positive_classes"] + aggregates["compatible_classes"] >= len(controls) - 1
    assert aggregates["disagreements_unexplained"] >= 1
    assert "module partition" not in aggregates
    assert comparison["module_partition"]["controls_compared"] >= 1


def test_comparison_reports_a_withheld_verdict_for_a_control_it_could_not_classify():
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(source)
    target = next(
        (control for control in result.controls if control.classification.kind == "unknown"),
        None,
    )
    if target is None:
        pytest.skip("this fixture produced no unclassified control")
    comparison = RP.compare_with_profile(
        result,
        _reference_profile(
            [
                {
                    "id": target.name,
                    "binding": {"index": target.index},
                    "type": "number",
                    "law": {"kind": "affine"},
                    "enum_values": [],
                    "range": {"min": 0.0, "max": 1.0},
                }
            ]
        ),
    )
    row = comparison["controls"][0]
    assert row["verdict"] == "withheld"
    assert "could not classify" in row["detail"]
    assert comparison["aggregates"]["withheld"] == 1
    assert comparison["aggregates"]["disagreements_unexplained"] == 0


def test_a_reference_enumeration_of_monotone_numbers_is_reported_as_a_ladder():
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(source)
    numeric = next(
        control
        for control in result.controls
        if control.mapping is not None
        and control.mapping.chosen is not None
        and control.classification.kind in ("ramp_like", "continuous")
    )
    comparison = RP.compare_with_profile(
        result,
        _reference_profile(
            [
                {
                    "id": numeric.name,
                    "binding": {"index": numeric.index},
                    "type": "enum",
                    "law": {"kind": "enum_index"},
                    "enum_values": [{"id": str(value)} for value in range(0, 64)],
                    "range": {"min": 0.0, "max": 63.0},
                }
            ]
        ),
    )
    row = comparison["controls"][0]
    assert row["verdict"] == "disagreement_explained"
    assert "monotone numeric series" in row["detail"]
    assert comparison["aggregates"]["disagreements_unexplained"] == 0


def test_scenario_18_a_provider_overlay_changes_the_answer_not_the_inference():
    """Overlay = the same inferred controls read against a reference that supplies
    structure. The inferred modules must be untouched by it."""
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(source)
    before = {node.id: list(node.controls) for node in result.grouping.nodes}
    profile_rows = [
        {
            "id": control.name,
            "binding": {"index": control.index},
            "type": "number",
            "law": {"kind": "affine"},
            "module": "Provider Says So",
            "enum_values": [],
            "range": {"min": 0.0, "max": 1.0},
        }
        for control in result.controls
    ]
    comparison = RP.compare_with_profile(result, _reference_profile(profile_rows))
    after = {node.id: list(node.controls) for node in result.grouping.nodes}
    assert before == after
    assert comparison["reference_modules"] == 1
    partition = comparison["module_partition"]
    assert partition["precision"] is not None and partition["recall"] is not None
    assert partition["recall"] < 1.0, "one reference module must conflict with the inferred partition"


def test_partition_agreement_is_exact_when_the_partitions_match():
    partition = {0: "a", 1: "a", 2: "b"}
    assert RP.partition_agreement(partition, dict(partition))["f1"] == 1.0
    confused = RP.partition_agreement({0: "a", 1: "a", 2: "b"}, {0: "a", 1: "b", 2: "b"})
    assert confused["f1"] == 0.0
    assert confused["pairs_only_in_discovery"] == 1 and confused["pairs_only_in_reference"] == 1
    empty = RP.partition_agreement({}, {})
    assert empty["f1"] is None and empty["pairs"] == 0


def test_a_count_equal_naming_difference_is_its_own_verdict(tmp_path):
    """Two counts are not two states. A run that calls a 4-band display `stepped`
    and a reference that calls it an enumeration is a *naming difference*: it must
    not be counted as agreement (found by the P26 independent review)."""
    document = {
        "kind": "recording",
        "identity": {"fx_name": "X", "class_id": "c", "module": "m", "param_count": 1},
        "grid": [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0],
        "controls": {
            "ratio": {"index": 0, "samples": ["1", "1", "2", "2", "4", "4", "8", "8", "8"]},
        },
    }
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    result = RP.replay_from_recorded(RP.read_recorded(path))
    control = result.controls[0]
    assert control.discrete.distinct_count == 4
    comparison = RP.compare_with_profile(
        result,
        _reference_profile(
            [
                {
                    "id": "ratio",
                    "binding": {"index": 0},
                    "type": "enum",
                    "law": {"kind": "enum_index"},
                    "enum_values": [{"id": label} for label in ("a", "b", "c", "d")],
                    "range": {"min": 0.0, "max": 3.0},
                }
            ]
        ),
    )
    row = comparison["controls"][0]
    assert row["verdict"] == "naming_difference", row["detail"]
    assert comparison["aggregates"]["naming_differences"] == 1
    assert comparison["aggregates"]["compatible_classes"] == 0
    assert comparison["aggregates"]["disagreements_unexplained"] == 0


def _banded_control(result) -> Any:
    """A control with at least three bands, so the *first* bracket cannot reach 1.0."""
    return next(
        (
            control
            for control in result.controls
            if control.discrete is not None
            and len(control.discrete.members) >= 3
            and control.discrete.members[1].upper < 0.9
        ),
        None,
    )


def test_boundary_containment_is_a_gating_state_not_a_decoration():
    """A measured switch point outside the bracket the replay reported must fail the
    row, and a row with no resolved transition must read `unchecked`.

    The bracket is the pair of *sampled* positions around the switch point (two grid
    steps wide at most). Comparing against the neighbouring band edges instead - the
    first version of this check - accepted almost any position, and the P26
    independent review is what caught it.
    """
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(source)
    control = _banded_control(result)
    assert control is not None, "the fixture must produce at least one banded control"

    def record(*, position: Any = None) -> dict:
        transition: dict[str, Any] = {"i": 1, "state": "exact"}
        if position is not None:
            transition["current"] = position
        return {
            "name": control.name,
            "index": control.index,
            "members": ["a", "b"],
            "transitions": [transition],
        }

    # a position that cannot lie in the bracket of the transition into band 1
    outside = RP.compare_with_boundaries(result, {"controls": [record(position=1.0)]})
    assert outside["counts"]["containment_failed"] >= 1
    failed = [row for row in outside["rows"] if row.get("containment") == "failed"]
    assert failed and failed[0]["measured_transitions_resolved"] > 0

    # transition 1 leaves band 0, so band 0 carries its bracket
    into_band_one = control.discrete.members[0].bracket[0]
    inside = RP.compare_with_boundaries(result, {"controls": [record(position=into_band_one)]})
    assert inside["counts"]["containment_failed"] == 0
    assert inside["counts"]["containment_strict"] + inside["counts"]["containment_within_slack"] >= 1

    unchecked = RP.compare_with_boundaries(result, {"controls": [record()]})
    assert unchecked["counts"]["containment_unchecked"] >= 1
    assert [row.get("containment") for row in unchecked["rows"]] == ["unchecked"]


def test_the_boundary_comparison_reports_strict_containment_separately():
    source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(source)
    control = _banded_control(result)
    assert control is not None
    measured = control.discrete.members[0].bracket[0]
    comparison = RP.compare_with_boundaries(
        result,
        {
            "controls": [
                {
                    "name": control.name,
                    "index": control.index,
                    "members": ["a", "b"],
                    "transitions": [{"i": 1, "state": "exact", "current": measured}],
                }
            ]
        },
    )
    counted = comparison["counts"]["containment_strict"] + comparison["counts"]["containment_within_slack"]
    assert counted == 1
    assert comparison["rows"][0]["checks"][0]["bracket_source"] == "band_upper_edge"
    row = comparison["rows"][0]
    if row["containment"] == "strict":
        assert row["strict_inside"] == row["measured_transitions_resolved"]


def test_band_structure_is_compared_against_a_recorded_boundary_measurement():
    discovery_source = RP.read_recorded(CALIBRATION_FIXTURE)
    result = RP.replay_from_recorded(discovery_source)
    boundaries = RP.read_recorded(BOUNDARY_FIXTURE)
    assert boundaries.shape == "boundary-record"
    comparison = RP.compare_with_boundaries(result, {"controls": boundaries.records})
    assert comparison["counts"]["band_count_agree"] + comparison["counts"]["band_count_differ"] > 0
    for row in comparison["rows"]:
        if row.get("verdict") == "band_count_agree":
            assert row["measured_transitions_resolved"] >= 0
        for check in row.get("checks") or []:
            if check.get("grid_bracket") and check.get("inside_grid_bracket") is False:
                assert row.get("unlabelled_bands") or row.get("verdict") == "band_count_differ"


# ---------------------------------------------------------------------------
# 20: the engine's place in the layering
# ---------------------------------------------------------------------------


def test_the_discovery_package_imports_no_profile_cache_or_provider_module():
    """The engine may be *consumed* by the profile path, never reach into it: a
    discovery run must not be able to read a cached profile or a provider."""
    package = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins" / "discovery"
    forbidden = ("import ..cache", "from ..cache", "from ..preload", "import ..preload", "from ..providers", "from ..resolve")
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path.name} reaches into {needle!r}"


def test_the_discovery_package_carries_no_provider_identifier():
    package = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins" / "discovery"
    for path in package.glob("*.py"):
        D.assert_no_provider_tokens(path.read_text(encoding="utf-8"), source=path.name)


# ---------------------------------------------------------------------------
# The machine's own recordings, when they exist (structural invariants only)
# ---------------------------------------------------------------------------


LIVE_CALIBRATION = Path(
    os.environ.get("LOCALAPPDATA", ""), "reaper_reapy_mcp", "calibration", "vital_host_calibration.json"
)


@pytest.mark.skipif(not LIVE_CALIBRATION.exists(), reason="no recorded live calibration on this machine")
def test_a_live_recording_replays_into_a_complete_structure():
    source = RP.read_recorded(LIVE_CALIBRATION)
    result = RP.replay_from_recorded(source)
    assert len(result.controls) == len(source.records)
    assert result.counts["modules"] >= 1
    assert result.counts["controls_observed"] == len(source.records)
    summary = REPORT.summarize(result)
    assert summary["unknown"]["by_reason"], "a real recording must leave some fields unresolved"
    # every control is either classified or has a recorded reason why not
    for control in result.controls:
        assert control.classification.kind != "unknown" or control.unknown
    assert result.cost.total_calls == 0
