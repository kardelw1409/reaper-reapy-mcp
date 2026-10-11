"""P11 — Decision 1: the rules Vital declares but does not evaluate.

Owner position (2026-10-10): do **not** implement the three live-flag rules by
guesswork, and do **not** change refusal behaviour for the appearance of
completeness. This file pins the consequence:

- the rules stay `evaluated: false`, and that status is visible in the profile
  and in every transaction result (dry-run and committed alike);
- a rule whose **subject** is decidable and whose deciding input is not is
  guarded: a plan that enters the subject is refused before any write, with the
  rule named and the missing input stated (`fm_needs_modulator`, and since the
  safety-gate correction of 2026-10-11 also `route_into_off_filter`, whose subject
  is a created `(source, destination)` pair - see `test_p12_safety_gates.py`);
- `wave_frame_needs_multiframe_wavetable` cannot be guarded from measured data
  (the table itself declares it informational), so it is disclosed, never fired;
- a plan that does not enter any guard is unaffected - the layer does not block
  operations whose safety provably does not depend on an unevaluated rule.

Mock/static only, plus an optional check against the real registry directory if
it is present on this machine (skipped otherwise).
"""

from __future__ import annotations

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import dependencies as D
from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins.errors import DependencyViolation, PlanError
from reaper_reapy_mcp.plugins.providers.vital import DEFAULT_REGISTRY_DIR, VitalProvider, load_registry
from reaper_reapy_mcp.plugins.providers.vital import codec as VC
from reaper_reapy_mcp.plugins.providers.vital import dependencies_rules as RULES

from mock_provider import MockProvider

import test_p7_vital as T
from test_p9_tools import call, demo  # noqa: F401 - the registered FastMCP surface

SUBJECT_SPELLING = "settings.Oscillator 1 Distortion Type"


# ---------------------------------------------------------------------------
# The honest status of the rules
# ---------------------------------------------------------------------------


def test_the_unevaluated_rules_are_declared_with_evaluated_false():
    rows = RULES.rules_not_evaluated()
    assert {row["rule"] for row in rows} == {
        "fm_needs_modulator",
        "route_into_off_filter",
        "wave_frame_needs_multiframe_wavetable",
    }
    for row in rows:
        assert row["evaluated"] is False, row
        assert row["needs"], row
        assert isinstance(row["blocking"], bool), row

    by_rule = {row["rule"]: row for row in rows}
    # the measured table's own kind/informational flags decide "blocking"
    assert by_rule["fm_needs_modulator"]["blocking"] is True
    assert by_rule["route_into_off_filter"]["blocking"] is True
    assert by_rule["wave_frame_needs_multiframe_wavetable"]["blocking"] is False


def test_two_rules_have_an_enforceable_guard_and_the_rest_is_honest():
    """Both blocking rules whose subject is decidable are guarded.

    `fm_needs_modulator`'s subject is a setting the profile binds; the route
    rule's subject is a created `(source, destination)` pair. The wavetable rule
    stays unguarded because the measured table itself declares it informational -
    refusing on it would harden the provider's own declaration (Decision 1).
    """
    by_rule = {row["rule"]: row for row in RULES.rules_not_evaluated()}
    assert by_rule["fm_needs_modulator"]["guard_enforceable"] is True
    assert by_rule["fm_needs_modulator"]["guard"]["kind"] == "subject_path_changed"
    assert by_rule["fm_needs_modulator"]["guard"]["decision"] == "refuse_before_any_write"
    assert by_rule["fm_needs_modulator"]["why_not_enforced"] is None

    assert by_rule["route_into_off_filter"]["guard_enforceable"] is True
    assert by_rule["route_into_off_filter"]["guard"]["kind"] == "route_pair_created"
    assert by_rule["route_into_off_filter"]["guard"]["decision"] == "refuse_before_any_write"
    assert by_rule["route_into_off_filter"]["why_not_enforced"] is None
    assert by_rule["route_into_off_filter"]["blocking"] is True

    rule = "wave_frame_needs_multiframe_wavetable"
    assert by_rule[rule]["guard_enforceable"] is False
    assert by_rule[rule]["guard"] is None
    assert by_rule[rule]["why_not_enforced"]


def test_the_guard_subjects_are_derived_from_the_measured_table():
    subjects = RULES.distortion_type_subjects()
    assert subjects == (
        "Oscillator 1 Distortion Type",
        "Oscillator 2 Distortion Type",
        "Oscillator 3 Distortion Type",
    )
    # derived, not typed: every subject is a key of the measured DISABLE table
    assert all(name in RULES.DISABLE_CANDIDATES for name in subjects)


def test_the_rule_rows_in_the_profile_stay_evaluated_false():
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    rows = {row["id"]: row for row in profile["dependencies"]}
    assert rows["fm_needs_modulator"]["evaluated"] is False
    assert rows["route_into_off_filter"]["evaluated"] is False
    assert rows["wave_frame_needs_multiframe_wavetable"]["evaluated"] is False
    assert rows["direct_out_bypasses_filters"]["evaluated"] is True
    assert any("not evaluated" in line for line in profile["limits"])


# ---------------------------------------------------------------------------
# The guard: refuse the subject, leave everything else alone
# ---------------------------------------------------------------------------


def guarded_profile() -> dict:
    """A profile that binds the guarded subject to a measured state path."""
    return {
        "controls": [
            {
                "id": "osc_1_distortion_type",
                "label": "Oscillator 1 Distortion Type",
                "aliases": ["Osc 1 Distortion Type"],
                "binding": {"state_key": "osc_1_distortion_type"},
                "law": {"kind": "none", "params": {}},
            }
        ]
    }


def findings_for(changed: dict) -> list:
    rules = RULES.dependencies(guarded_profile())
    return rules.evaluate({"state_paths_changed": changed, "controls_changed": {}}, rules.rules)


def test_the_guard_fires_on_the_measured_path_spelling():
    findings = findings_for({"settings.osc_1_distortion_type": 2.0})
    assert len(findings) == 1
    row = findings[0]
    assert row.rule == "fm_needs_modulator"
    assert row.kind == "hard" and row.informational is False
    assert row.detail["evaluated"] is False
    assert row.detail["reason"] == "rule_not_evaluated"
    assert row.detail["missing_input"]
    assert row.detail["observed"] == 2.0


def test_the_guard_fires_on_the_subject_name_spelling_too():
    """The structural DISABLE set used to spell exactly this path."""
    findings = findings_for({SUBJECT_SPELLING: "FM"})
    assert [row.rule for row in findings] == ["fm_needs_modulator"]


def test_the_guard_stays_silent_for_everything_that_is_not_its_subject():
    assert findings_for({"settings.filter_1_cutoff": 40.0}) == []
    assert findings_for({"settings.mode": "mono"}) == []
    assert findings_for({"settings.reverb_on": 0.0}) == []
    # turning the *other* DISABLE candidates off is not the guarded subject
    assert findings_for({"settings.osc_1_distortion_amount": 0.0}) == []


def test_a_plan_entering_the_guard_is_refused_before_any_write():
    host = T.vital_host()
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    registry = T.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T.IDENTITY
    )
    request = {"state": {SUBJECT_SPELLING: "FM"}}
    before = host.slot(0, 0).block

    def run(dry_run: bool):
        return T.Transaction(
            provider=provider,
            codec=provider.codec(),
            bridge=host,
            profile=profile,
            ref=ref,
            request=dict(request),
            token=token,
            registry=registry,
            dry_run=dry_run,
        ).run()

    for dry_run in (True, False):
        result = run(dry_run)
        assert result.ok is False, result.data
        assert isinstance(result.error, DependencyViolation)
        assert "unevaluable hard dependency rule(s)" in result.error.message
        row = result.error.detail["findings"][0]
        assert row["rule"] == "fm_needs_modulator"
        assert row["evaluated"] is False
        assert row["reason"] == "rule_not_evaluated"
        assert result.error.detail["unevaluable"] == ["fm_needs_modulator"]
        assert result.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE
        assert result.outcome.host.write_calls_attempted == 0
        # no side effect at all: no write verb was called and the document is byte-identical
        assert [label for label, _d in host.log if label.startswith("write")] == []
        assert host.slot(0, 0).block == before


def test_a_dry_run_is_not_a_fake_success_when_the_real_run_refuses():
    """The dry run must refuse the same request identically (Decision 1)."""
    host = T.vital_host()
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    registry = T.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T.IDENTITY
    )
    preview = T.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request={"state": {SUBJECT_SPELLING: "FM"}},
        token=token,
        registry=registry,
        dry_run=True,
    ).run()
    assert preview.ok is False
    assert preview.data.get("would_write") is not True
    assert preview.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE


def test_an_unrelated_write_still_works_and_discloses_what_was_not_checked():
    """Non-vacuity: the guard must not block the layer's ordinary operations."""
    host = T.vital_host()
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    registry = T.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T.IDENTITY
    )
    result = T.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request={"state": {"settings.chorus_delay_1": -8.0}},
        token=token,
        registry=registry,
        dry_run=True,
    ).run()
    assert result.ok is True, result.error
    assert result.data["would_write"] is True
    disclosed = {row["rule"]: row for row in result.data["dependency_rules_not_evaluated"]}
    assert disclosed["fm_needs_modulator"]["evaluated"] is False
    assert disclosed["fm_needs_modulator"]["guard_enforceable"] is True
    assert disclosed["route_into_off_filter"]["guard_enforceable"] is True
    assert disclosed["route_into_off_filter"]["guard"]["kind"] == "route_pair_created"
    assert disclosed["wave_frame_needs_multiframe_wavetable"]["guard_enforceable"] is False
    assert disclosed["wave_frame_needs_multiframe_wavetable"]["why_not_enforced"]


def test_the_disclosure_survives_a_refusal_too():
    host = T.vital_host()
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    registry = T.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T.IDENTITY
    )
    result = T.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request={"state": {"settings.sample.samples": "AAAA"}},
        token=token,
        registry=registry,
        dry_run=False,
    ).run()
    assert result.ok is False
    rows = result.data["dependency_rules_not_evaluated"]
    assert {row["rule"] for row in rows} == {
        "fm_needs_modulator",
        "route_into_off_filter",
        "wave_frame_needs_multiframe_wavetable",
    }


# ---------------------------------------------------------------------------
# The structural DISABLE set: measured paths or a named refusal (never phantom)
# ---------------------------------------------------------------------------


def test_disable_targets_refuse_rather_than_invent_a_document_path():
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    targets = {row["candidate"]: row for row in RULES.disable_targets(profile)}
    assert set(targets) == set(RULES.DISABLE_CANDIDATES)
    for name, row in targets.items():
        assert "path" not in row, name
        assert row.get("reason") == "disable_target_unmapped", name
        assert row["detail"], name


def test_the_codec_would_create_an_unknown_key_so_the_refusal_is_the_fix():
    """Why the mapping matters: `set_path` creates keys that do not exist.

    This is the mechanism behind the measured phantom-key defect: had the
    provider kept spelling display names as paths, the write would have landed -
    in keys the plugin never reads - and the transaction would have verified
    against them.
    """
    view = T.document()
    VC.set_path(view, SUBJECT_SPELLING, "FM")
    assert view["settings"]["Oscillator 1 Distortion Type"] == "FM"
    assert "osc_1_distortion_type" not in view["settings"]


def test_the_keep_on_semantics_of_the_disable_set_are_not_inert():
    """`str(control_dict)` never matched a candidate name; normalised names do."""
    planned = ["Oscillator 1 Distortion Type", "reverb_switch"]
    keep = RULES.disable_set(planned)
    assert "Oscillator 1 Distortion Type" not in keep
    assert "Reverb Switch" not in keep
    assert "Delay Switch" in keep


def test_the_enum_display_branch_converts_to_the_member_index():
    """A subject whose control declares a measured enum law converts its display
    off value to the member index - the space the live document uses (2026-10-11).

    This replaces the pre-derivation branch, which emitted the *display string*
    as the state value: a string in a numeric leaf is the phantom-value shape the
    DISABLE table exists to prevent, so the conversion is now required and the
    unconvertible cases are refused (see the two probes above).
    """
    profile = {
        "controls": [
            {
                "id": "osc_1_distortion_type",
                "label": "Oscillator 1 Distortion Type",
                "type": "enum",
                "enum_values": [{"id": "None", "label": "None"}, {"id": "Sync", "label": "Sync"}],
                "binding": {"state_key": "osc_1_distortion_type"},
                "law": {"kind": "enum_index", "params": {}},
            }
        ]
    }
    rows = {row["candidate"]: row for row in RULES.disable_targets(profile)}
    row = rows["Oscillator 1 Distortion Type"]
    assert row["path"] == "settings.osc_1_distortion_type"
    assert row["value"] == 0.0
    assert row["value_space"] == "native"
    assert row["value_from_display"] == "None"
    assert isinstance(row["value"], float), "a numeric leaf must not receive a string"


def test_the_provider_report_fallback_is_reachable():
    """If a provider's unevaluated-rule report raises, the result says so instead
    of silently claiming there is nothing to disclose (audit finding)."""

    class BrokenReport(MockProvider):
        def unevaluated_rules(self):
            raise PlanError("the report is unavailable")

    host = T.vital_host()
    provider = BrokenReport()
    facts = host.fx_facts(0, 0)
    registry = T.TargetTokenRegistry()
    result = T.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=M.PluginInstanceRef(
            host="reaper", location={"track_index": 0, "fx_index": 0}, provider="demo", identity={}
        ),
        request={},
        token=registry.issue(facts=facts),
        registry=registry,
        dry_run=True,
    ).run()
    rows = result.data["dependency_rules_not_evaluated"]
    assert len(rows) == 1
    assert rows[0]["evaluated"] is False
    assert rows[0]["reason"] == "unevaluated_rule_report_unavailable"


def test_a_committed_write_discloses_the_unevaluated_rules_too():
    """The disclosure is on the committed path as well (audit finding).

    It was pinned on the dry-run and refusal paths; a committed write is the one a
    caller is most likely to trust, so it must carry the same statement about what
    was **not** checked.
    """
    host = T.vital_host()
    provider = T.provider_on_fixtures()
    profile = provider.build_profile(dict(T.IDENTITY))
    registry = T.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    result = T.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=M.PluginInstanceRef(
            host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T.IDENTITY
        ),
        request={"state": {"settings.chorus_delay_1": -8.0}},
        token=token,
        registry=registry,
        dry_run=False,
    ).run()
    assert result.ok is True, result.error
    assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
    rows = {row["rule"]: row for row in result.data["dependency_rules_not_evaluated"]}
    assert rows["fm_needs_modulator"]["evaluated"] is False
    assert rows["fm_needs_modulator"]["blocking"] is True
    assert rows["route_into_off_filter"]["guard_enforceable"] is True


def test_the_mutation_surface_in_the_core_carries_the_disclosure():
    """`modulation._mutation_data` used to rebuild the findings and drop the list."""
    from mock_provider import MockProvider

    class ReportingProvider(MockProvider):
        def unevaluated_rules(self):
            return [
                {
                    "rule": "demo_live_flag_rule",
                    "evaluated": False,
                    "blocking": True,
                    "needs": "a live flag the projected world does not carry",
                    "guard": None,
                    "guard_enforceable": False,
                    "why_not_enforced": "no enforceable subject test exists",
                }
            ]

    host = T.vital_host()
    provider = ReportingProvider()
    facts = host.fx_facts(0, 0)
    registry = T.TargetTokenRegistry()
    result = _modulation(
        provider=provider,
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=M.PluginInstanceRef(
            host="reaper", location={"track_index": 0, "fx_index": 0}, provider="demo", identity={}
        ),
        token=registry.issue(facts=facts),
        registry=registry,
        op="create",
        routes=[{"source": "l1", "destination": "d1"}],
        dry_run=True,
    )
    rows = result.data["dependency_rules_not_evaluated"]
    assert [row["rule"] for row in rows] == ["demo_live_flag_rule"]
    assert rows[0]["evaluated"] is False
    assert result.data["force"] is False


def _modulation(**fields):
    from reaper_reapy_mcp.plugins import modulation as MOD

    return MOD.modulation(**fields)


def test_the_unevaluated_list_is_not_dropped_from_a_tool_mutation_response(demo):  # noqa: F811
    """The registered surface, end to end: the list must survive the envelope."""
    from mock_provider import MockProvider as _MockProvider

    class Reporting(_MockProvider):
        def unevaluated_rules(self):
            return [
                {"rule": "demo_live_flag_rule", "evaluated": False, "blocking": True, "needs": "x"}
            ]

    demo["factory"]["make"] = lambda: Reporting()
    envelope = call(  # noqa: F821
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": {"track_index": 0, "fx_index": 1},
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}],
        },
    )
    assert envelope["ok"] is True, envelope
    rows = envelope["data"]["dependency_rules_not_evaluated"]
    assert [row["rule"] for row in rows] == ["demo_live_flag_rule"]
    assert rows[0]["evaluated"] is False


# ---------------------------------------------------------------------------
# The same rules against the REAL registry, when this machine has it
# ---------------------------------------------------------------------------


def real_profile():
    try:
        registry = load_registry(directory=str(DEFAULT_REGISTRY_DIR))
    except Exception:  # noqa: BLE001 - an absent registry is a skip, not a failure
        pytest.skip(f"the measured registry directory is not present: {DEFAULT_REGISTRY_DIR}")
    provider = VitalProvider(registry=registry)
    return provider.build_profile(
        {
            "fx_name": "VST3i: Vital (Vital Audio)",
            "class_id": "56535456697461766974616C00000000",
            "module": "Vital.vst3",
            "param_count": 2858,
            "version": "1.5.5",
        }
    )


def test_against_the_real_registry_the_guard_fires_on_the_measured_path():
    profile = real_profile()
    rules = RULES.dependencies(profile)
    world = {
        "state_paths_changed": {"settings.osc_1_distortion_type": 2.0},
        "controls_changed": {},
    }
    findings = rules.evaluate(world, rules.rules)
    assert [row.rule for row in findings] == ["fm_needs_modulator"]
    assert findings[0].detail["evaluated"] is False
    # and an ordinary cutoff write keeps working
    other = rules.evaluate(
        {"state_paths_changed": {"settings.filter_1_cutoff": 40.0}, "controls_changed": {}},
        rules.rules,
    )
    assert other == []


def test_against_the_real_registry_the_disable_targets_split_honestly():
    profile = real_profile()
    rows = {row["candidate"]: row for row in RULES.disable_targets(profile)}
    mapped = {name: row for name, row in rows.items() if not row.get("reason")}
    refused = {name: row for name, row in rows.items() if row.get("reason")}
    # every DISABLE candidate is bound by the profile's own declared names: all 13
    # can be addressed with a measured value in the document's own space
    assert set(mapped) | set(refused) == set(RULES.DISABLE_CANDIDATES)
    assert mapped["Reverb Switch"]["path"] == "settings.reverb_on"
    assert mapped["Reverb Switch"]["value"] == 0.0
    assert mapped["Reverb Switch"]["value_space"] == "native"
    assert mapped["Oscillator 1 Distortion Amount"]["path"] == "settings.osc_1_distortion_amount"
    assert mapped["Compressor Switch"]["path"] == "settings.compressor_on"
    # The distortion *types* are display-valued candidates. Since the measured
    # sweep derivation (2026-10-11) each of them declares a law with measured
    # members, so the display value "None" converts to its **member index** - the
    # space the captured live document demonstrably uses
    # (`osc_1_distortion_type = 0.0`, i.e. member 0 of the measured member list).
    # Before the derivation these three were refused
    # (`disable_value_not_measurable`), which is what made every structural change
    # refuse (L-9).
    assert set(refused) == set(), refused
    for slot in ("1", "2", "3"):
        name = f"Oscillator {slot} Distortion Type"
        row = mapped[name]
        assert row["value"] == 0.0
        assert row["value_space"] == "native"
        assert row["value_from_display"] == "None"
        assert row["path"] == f"settings.osc_{slot}_distortion_type"
        assert " " not in row["path"]


def test_a_display_off_value_without_a_measurable_law_is_still_refused(monkeypatch):
    """Non-vacuity: the refusal branch above must still be reachable.

    A candidate whose off value is a display string on a control with **no** law
    and no measured members cannot be written into the document in the document's
    own space - it must be named, not guessed.
    """
    profile = real_profile()
    controls = [item for item in profile["controls"] if item["id"] == "osc_1_distortion_type"]
    assert controls, "the real profile must carry the measured control"
    control = controls[0]
    control["type"] = "number"
    control["law"] = {"kind": "none", "params": {}}
    monkeypatch.setitem(
        RULES.DISABLE_CANDIDATES,
        "Oscillator 1 Distortion Type",
        {"off": "None", "why": "synthetic probe of the refusal branch"},
    )
    rows = {row["candidate"]: row for row in RULES.disable_targets(profile)}
    row = rows["Oscillator 1 Distortion Type"]
    assert row["reason"] == "disable_value_not_measurable", row
    assert row["path"] == "settings.osc_1_distortion_type"
    assert row["off"] == "None"


def test_a_display_off_value_on_a_non_enum_law_is_refused_too(monkeypatch):
    """The second honest refusal: native space is only *measured* for enum indices.

    A power-law control's display value is not the document's own space (its
    document leaf holds the internal taper value), so the conversion must not be
    invented - the candidate is refused with that reason.
    """
    profile = real_profile()
    control = next(item for item in profile["controls"] if item["id"] == "osc_1_level")
    assert control["law"]["kind"] == "power"
    monkeypatch.setitem(
        RULES.DISABLE_CANDIDATES,
        "Oscillator 1 Level",
        {"off": "None", "why": "synthetic probe of the non-enum display branch"},
    )
    rows = {row["candidate"]: row for row in RULES.disable_targets(profile)}
    row = rows["Oscillator 1 Level"]
    assert row["reason"] == "disable_value_not_measurable", row
    assert "does not establish the document's own space" in row["detail"]
