"""P6: modulation operations, dependency and depth hooks - entirely on mocks.

Covers the [10 §4] requirement set at this layer: read-side ambiguity vs. the
mutation refusal (B-48), hard dependencies refusing with zero writes, depth
refusals as first-class answers, the projected-world fixture, N routes => N
ordered verified units, the mutation-default audit (B-49) and the envelope shape
of the tool body. No live write is possible (the conftest gate).
"""

from __future__ import annotations

import json

import pytest

import fixtures as F
import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import dependencies as D
from reaper_reapy_mcp.plugins import modulation as MOD
from reaper_reapy_mcp.plugins import tools as plugin_tools
from reaper_reapy_mcp.plugins.errors import (
    DependencyViolation,
    InvalidRoute,
    InvalidValue,
    NoConversion,
    UnsupportedOverride,
)
from reaper_reapy_mcp.plugins.model import Finding, PluginInstanceRef, Verdict
from reaper_reapy_mcp.plugins.provider import DependencyRuleSet
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry

NEW_PAIRS = [
    {"source": "src_a", "destination": "tone_level"},
    {"source": "src_b", "destination": "tone_cutoff"},
    {"source": "src_a", "destination": "tone_level"},  # third op re-uses a pair
]


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def build_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, MH.MockFxSlot()])])


class RouteBudgetProvider(MockProvider):
    """A provider whose hard rule is route-sensitive (provider-evaluated)."""

    budget = 3

    def dependencies(self) -> DependencyRuleSet:
        budget = self.budget

        def evaluate(world, rules):  # noqa: ANN001
            active = int(world.get("active_routes", 0))
            if active > budget:
                return [
                    Finding(
                        kind="hard",
                        rule="route_budget",
                        subject={"active_routes": active},
                        message=f"a plan that ends with {active} active routes exceeds the budget {budget}",
                        detail={"projected_active_routes": active, "budget": budget},
                    )
                ]
            return []

        return DependencyRuleSet(rules=[{"id": "route_budget", "kind": "hard"}], evaluate=evaluate)


class RecordingProvider(MockProvider):
    """Records the provider-visible request so the body's pass-through is provable."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_requests: list[dict] = []

    def normalize_request(self, profile, request):  # noqa: ANN001
        self.seen_requests.append(json.loads(json.dumps(request)))
        return super().normalize_request(profile, request)


def call(host, provider, **kwargs):
    facts = host.fx_facts(0, 1)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": 1},
        provider=str(provider.id),
        identity=provider.identify(facts),
    )
    return MOD.modulation(
        provider=provider,
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        token=token,
        registry=registry,
        **kwargs,
    )


def routes_of(host: MH.MockHostBridge) -> list[dict]:
    document = F.decode_block_document(host.slot(0, 1).block)
    return document["routes"]


def write_labels(host: MH.MockHostBridge) -> list[str]:
    return [label for label, _detail in host.log if label.startswith("write")]


# ---------------------------------------------------------------------------
# Read-side semantics (deterministic first match + remaining, never a mutation)
# ---------------------------------------------------------------------------


def test_read_side_ambiguity_returns_first_match_and_the_rest():
    host = build_host()
    host.external_set_document(
        {
            **F.demo_document(),
            "routes": [
                {"slot": 1, "source": "src_a", "destination": "tone_cutoff", "amount": 0.25, "bipolar": False},
                {"slot": 2, "source": "src_a", "destination": "tone_cutoff", "amount": 0.5, "bipolar": True},
            ],
        },
        fx_index=1,
    )
    result = call(host, MockProvider(), op="list", selector={"source": "src_a"})
    assert result.ok
    assert result.data["n_matches"] == 2
    assert result.data["selected"]["slot"] == 1, "provider (document) order decides the first match"
    assert [row["slot"] for row in result.data["remaining"]] == [2]
    assert result.data["ambiguity"] is not None
    assert result.data["n_slots_total"] == F.ROUTE_SLOTS
    assert result.data["n_free"] == F.ROUTE_SLOTS - 2
    assert write_labels(host) == [], "a read never writes"


def test_list_by_slot_selects_exactly_that_route():
    host = build_host()
    result = call(host, MockProvider(), op="list", selector={"slot": 2})
    assert result.ok and result.data["n_matches"] == 1
    assert result.data["selected"]["destination"] == "tone_level"
    assert result.data["remaining"] == []


# ---------------------------------------------------------------------------
# P6-1: an ambiguous mutation selector is refused, with the candidates (B-48)
# ---------------------------------------------------------------------------


def test_p6_1_ambiguous_selector_refuses_a_mutation_with_zero_writes():
    host = build_host()
    host.external_set_document(
        {
            **F.demo_document(),
            "routes": [
                {"slot": 1, "source": "src_a", "destination": "tone_cutoff", "amount": 0.25, "bipolar": False},
                {"slot": 2, "source": "src_a", "destination": "tone_cutoff", "amount": 0.5, "bipolar": True},
            ],
        },
        fx_index=1,
    )
    before = json.dumps(routes_of(host), sort_keys=True)
    result = call(
        host,
        MockProvider(),
        op="update",
        selector={"source": "src_a"},
        routes=[{"amount": 0.9}],
        dry_run=False,
    )
    assert not result.ok
    assert result.error.code == "InvalidRoute"
    assert result.error.detail["reason"] == "ambiguous_selector"
    assert {row["slot"] for row in result.error.detail["candidates"]} == {1, 2}
    assert write_labels(host) == [], "an ambiguous mutation never reaches a write"
    assert json.dumps(routes_of(host), sort_keys=True) == before


def test_unknown_slot_and_empty_slot_refuse_typed():
    host = build_host()
    result = call(host, MockProvider(), op="remove", selector={"slot": 99}, dry_run=False)
    assert not result.ok and result.error.code == "InvalidRoute"
    assert result.error.detail["reason"] == "unknown_slot"

    # make slot 3 present but empty (no destination) in the table
    document = F.demo_document()
    document["routes"].append({"slot": 3, "source": "", "destination": "", "amount": 0.0, "bipolar": False})
    host.external_set_document(document, fx_index=1)
    empty = call(host, MockProvider(), op="remove", selector={"slot": 3}, dry_run=False)
    assert not empty.ok and empty.error.code == "InvalidRoute"
    assert empty.error.detail["reason"] == "empty_slot"
    assert write_labels(host) == []


def test_a_mutation_without_any_identity_refuses():
    host = build_host()
    result = call(host, MockProvider(), op="update", routes=[{"amount": 0.9}], dry_run=False)
    assert not result.ok and result.error.code == "InvalidRoute"
    assert result.error.detail["reason"] == "missing_selector"
    assert write_labels(host) == []


# ---------------------------------------------------------------------------
# P6-2: hard dependency refuses before the first write
# ---------------------------------------------------------------------------


def test_p6_2_hard_dependency_refuses_a_route_mutation_with_zero_writes():
    host = build_host()
    provider = RouteBudgetProvider()
    provider.budget = 2  # the live document already has two active routes
    result = call(
        host,
        provider,
        op="create",
        routes=[{"source": "src_a", "destination": "tone_level"}],
        dry_run=False,
    )
    assert not result.ok
    assert isinstance(result.error, DependencyViolation)
    assert result.error.detail["findings"][0]["rule"] == "route_budget"
    outcome = result.outcome
    assert outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert outcome.host.write_calls_attempted == 0
    assert outcome.host.zero_writes
    assert write_labels(host) == []
    assert result.data["dependency_findings"][0]["rule"] == "route_budget"


def test_p6_2b_projected_world_lets_a_correct_plan_through():
    """The same rule refuses the raw (unprojected) judgement but passes when the
    plan's own removals are projected in (10 §3.1)."""
    host = build_host()
    provider = RouteBudgetProvider()
    provider.budget = 3

    result = call(
        host,
        provider,
        op="create",
        routes=[{"source": "src_a", "destination": "tone_level"}],
        dry_run=True,
    )
    assert result.ok and not any(
        findings["kind"] == "hard" for findings in result.data["dependency_findings"]
    )

    raw = D.evaluate(provider.dependencies(), {"active_routes": 2 + 3})
    assert raw and raw[0].rule == "route_budget", (
        "non-vacuity: judging the raw snapshot (no projections) must produce the finding"
    )


# ---------------------------------------------------------------------------
# P6-3: depth conversion and its refusals
# ---------------------------------------------------------------------------


def test_p6_3_depth_refusal_is_a_first_class_answer_and_force_never_invents_a_factor():
    host = build_host()
    provider = MockProvider()
    result = call(host, provider, op="depth", destination="tone_cutoff", depth=12)
    assert not result.ok
    assert isinstance(result.error, NoConversion)
    assert result.error.detail["reason"] == "non_affine"
    assert "factor" not in json.dumps(result.data).lower()


def test_p6_3b_accepted_depth_reports_the_amount_law_and_evidence():
    host = build_host()
    result = call(host, MockProvider(), op="depth", destination="tone_level", depth=12)
    assert result.ok
    assert result.data["native_amount"] == 1.2
    assert result.data["law"] == {"kind": "identity", "params": {}}
    assert result.data["evidence"]["strength"] == "strong"
    assert result.data["force"] is False


def test_force_is_a_typed_refusal_not_an_echoed_permission():
    """Decision 4 (2026-10-10): V1 implements no override.

    The POC's ``force`` escape was declared but nothing honoured it, so a caller
    could believe a policy refusal had been overridden. It is now refused at the
    Core boundary as well as at the tool boundary, for **every** operation, and
    the refusal happens before any read or write of the instance state.
    """
    host = build_host()
    provider = RecordingProvider()
    for op in ("list", "depth", "create", "update", "remove"):
        with pytest.raises(UnsupportedOverride) as excinfo:
            call(host, provider, op=op, force=True)
        assert excinfo.value.detail["argument"] == "force"
        assert excinfo.value.detail["supported"] is False
        assert excinfo.value.detail["reason"] == "force_not_supported"
        assert excinfo.value.detail["op"] == op
    assert not provider.seen_requests, "the provider must never see a forced request"
    assert host.count("read_state_chunk") == 0
    assert write_labels(host) == []


def test_a_wrong_type_for_force_is_refused_rather_than_coerced():
    """``"false"`` is truthy: a string must never opt a caller into a force path."""
    host = build_host()
    for value in (1, "false", "true", [], {}):
        with pytest.raises(InvalidValue) as excinfo:
            call(host, MockProvider(), op="create", routes=[], force=value)
        assert excinfo.value.detail["argument"] == "force", value
        assert excinfo.value.detail["value"] == value
    assert host.count("read_state_chunk") == 0
    assert write_labels(host) == []


# ---------------------------------------------------------------------------
# P6-5: the dry-run default holds across the whole modulation surface
# ---------------------------------------------------------------------------


def test_p6_5_every_mutating_op_applies_nothing_without_explicit_dry_run_false():
    create_host = build_host()
    before = json.dumps(routes_of(create_host), sort_keys=True)
    created = call(
        create_host,
        MockProvider(),
        op="create",
        routes=[{"source": "src_a", "destination": "tone_level"}],
    )
    assert created.ok and created.data["dry_run"] is True
    assert created.outcome.host.zero_writes
    assert json.dumps(routes_of(create_host), sort_keys=True) == before

    for op, selector, routes in (
        ("update", {"slot": 1}, [{"amount": 0.9}]),
        ("remove", {"slot": 1}, None),
    ):
        host = build_host()
        snapshot = json.dumps(routes_of(host), sort_keys=True)
        result = call(host, MockProvider(), op=op, selector=selector, routes=routes)
        assert result.ok, (op, result.error)
        assert result.data["dry_run"] is True
        assert result.outcome.host.zero_writes, op
        assert write_labels(host) == [], op
        assert json.dumps(routes_of(host), sort_keys=True) == snapshot


# ---------------------------------------------------------------------------
# P6-4: N routes => N ordered, individually verified units
# ---------------------------------------------------------------------------


def test_p6_4_three_route_change_is_three_ordered_verified_units():
    host = build_host()
    profile = F.demo_profile()
    profile["modulation"]["destinations"] = ["tone_cutoff", "tone_level", "tone_shape"]
    provider = MockProvider(profile=profile)
    before = routes_of(host)
    result = call(
        host,
        provider,
        op="create",
        routes=[
            {"source": "src_a", "destination": "tone_shape"},
            {"source": "src_b", "destination": "tone_cutoff"},
            {"source": "src_a", "destination": "tone_level"},
        ],
        dry_run=False,
    )
    assert result.ok, result.error
    assert result.outcome.verdict is Verdict.ALL_VERIFIED
    writes = [row for row in host.log if row[0] == "write_state_slice"]
    assert len(writes) == 3, "one route per state unit (the declared structural limit)"
    assert result.outcome.host.write_calls_attempted == 3
    assert result.outcome.host.write_effects_verified == 3
    assert result.outcome.host.write_effects_unknown == 0

    landed = [route for route in routes_of(host) if route not in before]
    assert len(landed) == 3 and len({route["slot"] for route in landed}) == 3
    reported = result.data["route_plan"]["routes_create"]
    assert [row["resolved_slot"] for row in reported] == sorted(
        route["slot"] for route in landed
    ), "the reported slots come from the read-back, not from a prediction"
    assert all(row["resolved_slot"] is not None for row in reported)


def test_p6_4b_update_and_remove_land_and_are_verified():
    host = build_host()
    updated = call(
        host, MockProvider(), op="update", selector={"slot": 1}, routes=[{"amount": 0.75}], dry_run=False
    )
    assert updated.ok, updated.error
    assert updated.outcome.verdict is Verdict.ALL_VERIFIED
    assert routes_of(host)[0]["amount"] == 0.75

    removed = call(host, MockProvider(), op="remove", selector={"slot": 1}, dry_run=False)
    assert removed.ok, removed.error
    assert removed.outcome.verdict is Verdict.ALL_VERIFIED
    assert routes_of(host)[0]["destination"] == ""
    assert [route["destination"] for route in routes_of(host) if route["destination"]] == ["tone_level"]


def test_p6_a_selector_resolved_to_one_slot_does_not_smuggle_values_into_the_write():
    host = build_host()
    result = call(
        host,
        MockProvider(),
        op="update",
        selector={"source": "src_a", "destination": "tone_cutoff"},
        routes=[{"amount": 0.6}],
        dry_run=False,
    )
    assert result.ok, result.error
    route = routes_of(host)[0]
    assert route["amount"] == 0.6
    assert route["source"] == "src_a" and route["destination"] == "tone_cutoff"
    assert result.data["request"]["routes_update"] == [{"slot": 1, "amount": 0.6}]


# ---------------------------------------------------------------------------
# Envelope and the mutation-default audit (B-49)
# ---------------------------------------------------------------------------


def test_mutation_envelope_carries_the_outcome_and_never_a_token():
    host = build_host()
    result = call(
        host,
        MockProvider(),
        op="create",
        routes=[{"source": "src_a", "destination": "tone_level"}],
        dry_run=False,
    )
    envelope = result.to_envelope()
    assert envelope["ok"] is True and envelope["schema"] == plugin_tools.SCHEMA_VERSION
    assert envelope["outcome"]["verdict"] == Verdict.ALL_VERIFIED.value
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 1
    assert "target_token" not in json.dumps(envelope)

    refused = call(host, MockProvider(), op="update", routes=[{"amount": 1.0}], dry_run=False)
    refused_envelope = refused.to_envelope()
    assert refused_envelope["ok"] is False
    assert refused_envelope["error"]["code"] == "InvalidRoute"
    assert refused_envelope["outcome"]["verdict"] == Verdict.REFUSED_BEFORE_ANY_WRITE.value


def test_b49_mutation_default_audit_over_the_registered_specs():
    spec = plugin_tools.SPEC["plugin_modulation"]
    assert spec["destructive"] is True
    assert spec["mutating_ops"] == ("create", "update", "remove")
    assert spec["args"]["dry_run"]["default"] is True
    for name, tool_spec in plugin_tools.SPEC.items():
        if tool_spec["destructive"]:
            assert tool_spec["args"].get("dry_run", {}).get("default") is True, name
    docstring = plugin_tools.synthesize_docstring("plugin_modulation")
    assert "route" in docstring.lower()


def test_unknown_modulation_op_is_refused_typed():
    host = build_host()
    result = call(host, MockProvider(), op="frobnicate")
    assert not result.ok and result.error.code == "InvalidRoute"
    assert "frobnicate" in result.error.detail["op"]
    assert write_labels(host) == []
