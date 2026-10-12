"""P26 core: display text, evidence, the observation store, the prepared store.

Brief §17 scenario map for this file:

- **15** missing or invalid discovery artifacts (the store refuses and says why);
- **19** identity / engine-version / content-hash validation of a stored artifact;
- the sampling budget and the "no duplicate sampling" property (§6, §16);
- the display reader, including the two traps this round found in the recorded
  data (a ratio display, and a printed resolution that varies with magnitude).
"""

from __future__ import annotations

import json
import math

import pytest

from reaper_reapy_mcp.plugins.discovery import ENGINE_VERSION
from reaper_reapy_mcp.plugins.discovery import evidence as EV
from reaper_reapy_mcp.plugins.discovery import text as TX
from reaper_reapy_mcp.plugins.discovery.observe import ObservationStore, SamplingBudget, grid_positions
from reaper_reapy_mcp.plugins.discovery.pipeline import DiscoveryEngine, EngineConfig
from reaper_reapy_mcp.plugins.discovery.store import (
    ARTIFACT_KIND,
    PreparedStore,
    discovery_directory,
    identity_key,
)
from reaper_reapy_mcp.plugins.discovery.report import summarize

import discovery_double as D


# ---------------------------------------------------------------------------
# Display text
# ---------------------------------------------------------------------------


def test_parses_numbers_units_and_signs():
    assert TX.parse_display("-52 semitones").value == -52.0
    assert TX.parse_display("-52 semitones").unit_token == "semitones"
    assert TX.parse_display("12dB").value == 12.0
    assert TX.parse_display("50%").value == 50.0
    assert TX.parse_display("1,234.5 Hz").value == 1234.5
    assert TX.parse_display("1.234e-05 s").value == pytest.approx(1.234e-05)
    # the unicode minus and thin space are what a host may print
    assert TX.parse_display("\u221252\u2009Hz").value == -52.0


def test_ratio_displays_are_read_as_the_ratio_they_print():
    """Measured on the recorded data: a sync control prints `1/4`, `2/1`, `1/64`.

    Reading `1/64` as "the number 1, unit /64" both loses the value and turns a
    numeric ramp into a set of distinct labels (it classified as an enum before
    this rule existed).
    """
    quarter = TX.parse_display("1/4")
    assert quarter.kind == "numeric" and quarter.value == 0.25 and quarter.form == "fraction"
    assert TX.parse_display("4/1").value == 4.0
    assert TX.parse_display("3 / 16").value == pytest.approx(0.1875)
    assert TX.parse_display("1/0").kind == "label"


def test_a_comma_decimal_display_is_refused_rather_than_read_1000x_too_large():
    """`0,500` is one half in a comma-decimal locale and not a thousands group;
    a leading-zero group is not valid thousands formatting. Found by the P26
    independent review, and the answer is to refuse the ambiguous string."""
    assert TX.parse_display("0,500").kind == "label"
    assert TX.parse_display("0,125").kind == "label"
    # the valid thousands form keeps working
    assert TX.parse_display("1,234.5 Hz").value == 1234.5
    assert TX.parse_display("12,000").value == 12000.0


def test_labels_and_non_finite_text_are_not_numbers():
    for text in ("Notch Blend", "", "0:48", "inf", "nan", "-", "+"):
        parsed = TX.parse_display(text)
        assert not parsed.numeric, text


def test_unit_token_requires_every_sample_to_agree():
    assert TX.unit_token_of(["12dB", "24dB"]) == "dB"
    assert TX.unit_token_of(["12dB", "24"]) is None
    assert TX.unit_token_of(["12dB", "Notch"]) is None
    assert TX.unit_token_of([]) is None


def test_printed_resolution_uses_the_finest_printed_place():
    coarse = TX.printed_resolution([0.0, 10.0], ["0", "10"])
    assert coarse.tolerance == pytest.approx(0.5)
    assert coarse.relative_term is None
    fine = TX.printed_resolution([0.0, 10.0], ["0.000", "10.000"])
    assert fine.tolerance == pytest.approx(0.0005)


def test_printed_resolution_includes_half_a_float32_ulp_at_the_largest_magnitude():
    values = [1.0e7, 1.0e7 + 1.0]
    texts = [f"{value:.1f}" for value in values]
    resolution = TX.printed_resolution(values, texts)
    assert resolution.float32_term == pytest.approx(0.5 * 2 ** (23 - 23))
    assert resolution.tolerance >= resolution.float32_term


def test_relative_budget_appears_only_when_the_display_spans_orders_of_magnitude():
    spanning = TX.printed_resolution([0.015625, 4.0], ["1/64", "4/1"])
    assert spanning.relative_mode == "ratio" and spanning.relative_term is not None
    assert spanning.budget_for(0.015625) < spanning.budget_for(4.0)
    narrow = TX.printed_resolution([10.0, 12.0], ["10.00", "12.00"])
    assert narrow.relative_mode == "absolute" and narrow.relative_term is None
    assert narrow.budget_for(12.0) == pytest.approx(narrow.tolerance)
    magnitudes = TX.printed_resolution([20.0, 20000.0], ["20.0", "20000.0"])
    assert magnitudes.relative_mode == "magnitude"
    assert magnitudes.budget_for(20000.0) >= magnitudes.tolerance


def test_change_points_report_the_bracket_not_a_position():
    changes = TX.change_points([0.0, 0.5, 1.0], ["a", "a", "b"])
    assert changes == [{"between": [0.5, 1.0], "from_text": "a", "to_text": "b"}]


# ---------------------------------------------------------------------------
# Evidence and precedence
# ---------------------------------------------------------------------------


def test_evidence_rejects_vocabulary_outside_the_declared_sets():
    with pytest.raises(ValueError):
        EV.Evidence(source_kind="probably", strength="strong")
    with pytest.raises(ValueError):
        EV.Evidence(source_kind="measured", strength="very")
    with pytest.raises(ValueError):
        EV.Evidence(source_kind="measured", reason="because")
    assert EV.Evidence.unknown("api_does_not_expose").strength == "none"


def test_measured_evidence_is_weaker_than_a_declaration_unless_checked():
    assert EV.strength_for("accepted") == "strong"
    assert EV.strength_for("declared") == "strong"
    assert EV.strength_for("measured") == "medium"
    assert EV.strength_for("measured", checked=True) == "strong"
    assert EV.strength_for("derived") == "weak"
    assert EV.strength_for("heuristic") == "weak"
    assert EV.strength_for("unknown") == "none"


def test_a_weaker_claim_never_narrows_a_stronger_one():
    claims = [
        EV.Claim(value=[8.0, 136.0], source_kind="declared", source="host"),
        EV.Claim(value=[12.0, 100.0], source_kind="measured", source="formatter", strength="strong"),
    ]
    bounds, conflict = EV.resolve_range("range", claims)
    assert bounds == (8.0, 136.0)
    assert conflict is not None and conflict.rule == "strongest_widest_wins"
    assert len(conflict.claims) == 2


def test_equally_strong_claims_widen_and_are_reported():
    claims = [
        EV.Claim(value=[0.0, 100.0], source_kind="declared", source="a"),
        EV.Claim(value=[-9.0, 100.0], source_kind="declared", source="b"),
    ]
    bounds, conflict = EV.resolve_range("range", claims)
    assert bounds == (-9.0, 100.0)
    assert conflict is not None


def test_scalar_resolution_reports_a_tie_it_cannot_settle():
    claims = [
        EV.Claim(value="affine", source_kind="measured", source="a"),
        EV.Claim(value="power", source_kind="measured", source="b"),
    ]
    value, conflict = EV.resolve_scalar("mapping", claims)
    assert value == "affine" and conflict is not None
    assert conflict.rule == "strongest_wins_ties_reported"
    one_sided = [*claims, EV.Claim(value="unknown", source_kind="heuristic", source="c")]
    value, conflict = EV.resolve_scalar("mapping", one_sided)
    assert value == "affine" and conflict is not None and conflict.rule == "strongest_wins_ties_reported"


# ---------------------------------------------------------------------------
# Observation store
# ---------------------------------------------------------------------------


def test_observation_store_never_samples_the_same_position_twice():
    calls: list[tuple[int, float]] = []

    def sampler(index: int, value: float) -> str:
        calls.append((index, value))
        return f"{value:.2f}"

    store = ObservationStore(sampler=sampler)
    assert store.observe(0, 0.5, stage="grid") == "0.50"
    assert store.observe(0, 0.5, stage="mapping") == "0.50"
    assert len(calls) == 1
    assert store.calls_by_stage == {"grid": 1}


def test_a_probe_one_ulp_from_a_known_position_is_probed_not_served_from_the_cache():
    """The dedupe key is the exact double. Rounding it (to 12 decimals, say) makes
    a bisection probe one ulp below a sampled position read the *other* text, which
    attributes a boundary to a position ~9000 ulps away while reporting `exact`."""
    probed: list[float] = []

    def sampler(index: int, value: float) -> str:
        probed.append(value)
        return "low" if value < 0.375 else "high"

    store = ObservationStore(sampler=sampler)
    store.observe(0, 0.375, stage="grid")
    below = math.nextafter(0.375, 0.0)
    assert store.observe(0, below, stage="refine") == "low"
    assert probed[-1] == below

    outcome = store.refine(0, 0.0, 1.0, low_text="low", high_text="high", stage="refine")
    low, high = outcome["bracket"]
    assert outcome["state"] == "exact"
    # the switch point is the exactly representable 0.375, so the bracket must end there
    assert high >= 0.375
    assert math.nextafter(low, high) >= high


def test_observation_store_stops_at_its_budget_and_says_so():
    store = ObservationStore(sampler=lambda index, value: "x", budget=SamplingBudget(max_calls=2))
    assert store.observe(0, 0.0, stage="grid") is not None
    assert store.observe(0, 0.5, stage="grid") is not None
    assert store.observe(0, 1.0, stage="grid") is None
    assert store.limits_hit == ["max_calls"]
    assert store.budget_left() == 0


def test_per_control_budget_is_separate_from_the_total_budget():
    store = ObservationStore(
        sampler=lambda index, value: "x",
        budget=SamplingBudget(max_calls=100, max_calls_per_control=1),
    )
    assert store.observe(0, 0.0, stage="grid") is not None
    assert store.observe(0, 0.5, stage="grid") is None
    assert store.limits_hit == ["max_calls_per_control"]
    assert store.observe(1, 0.5, stage="grid") is not None


def test_a_formatter_that_answers_nothing_is_recorded_as_absent_not_as_text():
    store = ObservationStore(sampler=lambda index, value: None)
    assert store.observe(0, 0.0, stage="grid") is None
    entry = store.entry(0)
    assert entry is not None and entry.status == "formatter_absent" and entry.samples == []


def test_refine_bisects_until_the_bracket_is_adjacent_doubles():
    threshold = 0.375
    store = ObservationStore(sampler=lambda index, value: "low" if value < threshold else "high")
    outcome = store.refine(0, 0.0, 1.0, low_text="low", high_text="high", stage="refine")
    assert outcome["state"] == "exact"
    low, high = outcome["bracket"]
    assert low < threshold <= high
    # "exact" must mean what it says: no representable value lies between the ends.
    assert math.nextafter(low, high) >= high
    assert outcome["calls"] > 10


def test_refine_never_reports_a_stalled_bracket_as_exact():
    """A midpoint that cannot split the interval is a *bracket*, not an answer.

    `(left + right) / 2` stalls tens of ulps early on values near 1.0, which used
    to be reported as an adjacent-double bracket. The state now says which one it
    is, and the bracket is checked against `nextafter` rather than assumed.
    """
    store = ObservationStore(
        sampler=lambda index, value: "low" if value < 0.375 else "high",
        budget=SamplingBudget(bisection_steps=6, min_bracket=0.0),
    )
    outcome = store.refine(0, 0.0, 1.0, low_text="low", high_text="high", stage="refine")
    assert outcome["state"] == "bracketed"
    low, high = outcome["bracket"]
    assert math.nextafter(low, high) < high
    assert "steps" in outcome or outcome["calls"] > 0


def test_refine_reports_a_budget_it_could_not_finish():
    store = ObservationStore(
        sampler=lambda index, value: "low" if value < 0.375 else "high",
        budget=SamplingBudget(bisection_steps=2),
    )
    outcome = store.refine(0, 0.0, 1.0, low_text="low", high_text="high", stage="refine")
    assert outcome["state"] == "bracketed"
    assert "budget" in str(outcome.get("reason"))
    assert store.limits_hit == ["bisection_steps"]


def test_refine_refuses_a_third_label_inside_the_bracket():
    def sampler(index: int, value: float) -> str:
        if value < 0.3:
            return "a"
        if value < 0.6:
            return "middle"
        return "b"

    store = ObservationStore(sampler=sampler)
    outcome = store.refine(0, 0.0, 1.0, low_text="a", high_text="b", stage="refine")
    assert outcome["state"] == "unresolved"
    assert outcome["middle_text"] == "middle"


def test_grid_positions_includes_both_ends():
    assert grid_positions(17)[0] == 0.0
    assert grid_positions(17)[-1] == 1.0
    assert len(grid_positions(17)) == 17


# ---------------------------------------------------------------------------
# Prepared artifacts
# ---------------------------------------------------------------------------


def _run(*, mode: str = "prepare") -> tuple[object, D.FakeDiscoveryBridge]:
    controls = [
        D.FakeControl(0, "alpha_gain", display=D.linear_display(0.0, 100.0, "%")),
        D.FakeControl(1, "alpha_mode", display=D.labels_display(["low", "mid", "high"])),
        D.FakeControl(2, "alpha_on", display=D.labels_display(["off", "on"]), is_toggle=True, step=1.0),
    ]
    bridge = D.FakeDiscoveryBridge(controls)
    facts = D.facts_for(param_count=len(controls))
    engine = DiscoveryEngine(bridge=bridge)
    result = engine.run(ref="ref", facts=facts, config=EngineConfig(mode=mode, grid_points=9).resolved())
    return result, bridge


def test_prepared_store_round_trip_verifies_the_bytes_it_wrote(tmp_path):
    result, _bridge = _run()
    store = PreparedStore(directory=tmp_path / "discovery")
    saved = store.save(result)
    assert saved["sha256_matches_disk"] is True
    assert saved["observations"] > 0

    artifact = store.load(result.identity)
    assert artifact.ok, artifact.reason
    assert artifact.meta()["mode"] == "prepare"
    assert artifact.payload()["counts"] == result.counts


def test_prepared_store_is_addressed_by_identity_and_refuses_a_misfiled_copy(tmp_path):
    """Two different defences, both needed: the path is keyed by identity, *and*
    a file that sits at the wrong key is refused by its own recorded identity."""
    result, _bridge = _run()
    store = PreparedStore(directory=tmp_path / "discovery")
    saved = store.save(result)
    other = dict(result.identity)
    other["fx_name"] = "Another Synth"
    assert store.load(other).status == "absent"
    misfiled = store.path_for(other)
    misfiled.write_bytes(open(saved["path"], "rb").read())
    artifact = store.load(other)
    assert artifact.status == "refused" and artifact.rule == "identity"
    assert "different instance" in artifact.reason


def test_prepared_store_refuses_a_changed_algorithm_generation(tmp_path):
    result, _bridge = _run()
    store = PreparedStore(directory=tmp_path / "discovery")
    store.save(result)
    artifact = store.load(result.identity, engine_version=ENGINE_VERSION + "-next")
    assert artifact.status == "absent"  # the key itself moves with the engine version
    assert artifact.path.endswith(".json")


def test_prepared_store_detects_a_tampered_payload(tmp_path):
    result, _bridge = _run()
    directory = tmp_path / "discovery"
    store = PreparedStore(directory=directory)
    saved = store.save(result)
    document = json.loads(open(saved["path"], encoding="utf-8").read())
    document["payload"]["counts"]["parameters"] = 999
    open(saved["path"], "w", encoding="utf-8", newline="\n").write(json.dumps(document))
    artifact = store.load(result.identity)
    assert artifact.status == "corrupt" and artifact.rule == "content_sha256"
    assert store.refusals and store.refusals[-1]["rule"] == "content_sha256"


def test_prepared_store_reports_absent_rather_than_guessing(tmp_path):
    store = PreparedStore(directory=tmp_path / "nothing-here")
    artifact = store.load({"fx_name": "x", "class_id": "c", "module": "m", "param_count": 1})
    assert artifact.status == "absent" and not artifact.ok
    assert "no prepared artifact" in artifact.reason


def test_prepared_store_export_and_import_validate_exactly_like_a_local_read(tmp_path):
    result, _bridge = _run()
    store = PreparedStore(directory=tmp_path / "discovery")
    store.save(result)
    exported = store.export(result.identity, tmp_path / "export" / "artifact.json")
    assert exported["status"] == "ok"

    fresh = PreparedStore(directory=tmp_path / "other")
    imported = fresh.import_file(exported["path"], identity=result.identity)
    assert imported.ok, imported.reason
    assert fresh.load(result.identity).ok

    # a corrupted copy is refused on import, with the rule that refused it
    broken = json.loads(open(exported["path"], encoding="utf-8").read())
    broken["schema_version"] = 99
    (tmp_path / "export" / "broken.json").write_text(json.dumps(broken), encoding="utf-8")
    refused = PreparedStore(directory=tmp_path / "third").import_file(tmp_path / "export" / "broken.json")
    assert refused.status == "refused" and refused.rule == "schema_version"


def test_import_refuses_to_overwrite_a_different_artifact_for_the_same_identity(tmp_path):
    result, _bridge = _run()
    store = PreparedStore(directory=tmp_path / "discovery")
    store.save(result)
    document = json.loads(open(store.path_for(result.identity), encoding="utf-8").read())
    document["identity"]["fx_name"] = result.identity["fx_name"]
    document["content_sha256"] = "0" * 64
    source = tmp_path / "foreign.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    outcome = store.import_file(source, identity=result.identity)
    assert outcome.status in ("corrupt", "refused")


def test_discovery_directory_is_user_local_and_can_be_overridden(tmp_path):
    default = discovery_directory({})
    assert "reaper_reapy_mcp" in str(default) and "discovery" in str(default)
    override = tmp_path / "elsewhere"
    assert discovery_directory({"REAPER_MCP_PLUGIN_DISCOVERY": str(override)}) == override
    with pytest.raises(ValueError):
        discovery_directory({"REAPER_MCP_PLUGIN_DISCOVERY": "   "})


def test_identity_key_moves_with_the_engine_version():
    identity = {"fx_name": "a", "class_id": "b", "module": "c", "param_count": 3}
    assert identity_key(identity) != identity_key(identity, engine_version="other")


def test_the_artifact_declares_its_kind_and_schema(tmp_path):
    result, _bridge = _run()
    store = PreparedStore(directory=tmp_path / "discovery")
    saved = store.save(result)
    document = json.loads(open(saved["path"], encoding="utf-8").read())
    assert document["kind"] == ARTIFACT_KIND
    assert document["schema_version"] == 1
    assert document["engine_version"] == ENGINE_VERSION


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_summary_separates_established_from_unknown_and_reports_cost():
    result, _bridge = _run()
    summary = summarize(result)
    assert summary["parameters_inventoried"] == 3
    assert summary["controls_analyzed"] == 3
    assert summary["classes"]
    assert summary["cost"]["total_calls"] > 0
    assert set(summary["unknown"]) >= {"total", "by_reason", "examples"}
