"""P12 — the two safety gates of the owner's correction (2026-10-11).

**Gate 1 — `route_into_off_filter`.** The rule is declared but not evaluated, so
a plan that **creates** a `(source, destination)` pair cannot be shown to satisfy
it. Creating a pair is refused before the first write, from both mutation
surfaces and through every representation the state channel offers; operations
that create no pair (removals, amount-only updates, clearing a field, reads) are
deliberately untouched, because their safety provably does not depend on the rule.

**Gate 2 — instance continuity (L-3).** A caller-supplied write-target token
claims *the instance that was validated*. The measured host cannot back that
claim (an FX removed and re-added at the same slot reproduced the same handle and
the same build facts; the artifact records no exposed datum that separates them),
so a mutation carrying one is refused before any host work. A read keeps
accepting it, and a location-addressed mutation stays available: its safety is
established inside the call (capture -> revalidate -> write -> verify -> restore)
and does not depend on cross-call continuity.

Mock/static only: no live host, no REAPER.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import resolve as RS
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins.errors import DependencyViolation, IdentityContinuityUnprovable
from reaper_reapy_mcp.plugins.providers.vital import dependencies_rules as RULES
from reaper_reapy_mcp.plugins.providers.vital import codec as VC

import test_p7_vital as T7
from test_p9_tools import build_host, call, demo  # noqa: F401 - the registered surface

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 1}
WRITE_VERBS = ("write_params", "write_state_slice")


def write_verbs(host) -> list[str]:
    return [label for label, _detail in host.log if label in WRITE_VERBS]


def preload_provider():
    """The demo provider hosting the **real** Vital rule table (mock host).

    The gate lives in the provider's dependency evaluator, so an MCP-level test
    needs a provider that declares that table; everything else (host, codec,
    profile) stays the demo mock. The rule table, its guard and its findings are
    the shipped ones, imported - never re-typed here.
    """

    class RouteRuleProvider(MockProvider):
        def dependencies(self):
            return RULES.dependencies(self._profile or {})

    return RouteRuleProvider()


# ---------------------------------------------------------------------------
# Gate 1 — the guard's subject test is the codec's own representation
# ---------------------------------------------------------------------------


def test_the_guard_recognises_exactly_the_paths_the_codec_writes():
    """Non-vacuity: the guard is derived from the code that owns the layout.

    If `make_routing`/`clear_routing` ever wrote a different shape, this fails -
    a guard built from a hand-typed list would keep passing.
    """
    for path in VC.make_routing(T7.document(), "lfo_1", "filter_1_cutoff", 0.0625, slot=3):
        # the filter is derived from the codec's own table name, not typed here: a
        # literal would stop checking the day the codec renamed the table
        if f".{VC.ROUTE_TABLE_KEY}." in path:
            assert VC.parse_route_pair_path(path) is not None, path
            assert RULES.addresses_route_table(path) is True, path
    for path in VC.clear_routing(T7.document(), 3):
        if f".{VC.ROUTE_TABLE_KEY}." in path:
            assert VC.parse_route_pair_path(path) is not None, path
    # the per-slot amount field is NOT in the route table (an amount-only change
    # is not the rule's subject)
    assert RULES.addresses_route_table(f"{VC.MUTABLE_ROOT}.modulation_3_amount") is False
    assert VC.parse_route_pair_path(f"{VC.MUTABLE_ROOT}.modulation_3_amount") is None
    # the table itself and a whole slot are addressed (writing them can create pairs)
    assert RULES.addresses_route_table(f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}") is True
    assert RULES.addresses_route_table(f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.3") is True


def rule_findings(world: dict) -> list:
    ruleset = RULES.dependencies({"controls": []})
    return [
        finding
        for finding in ruleset.evaluate(world, ruleset.rules)
        if finding.rule == "route_into_off_filter"
    ]


def test_the_guard_fires_on_every_way_a_pair_can_be_created():
    cases = [
        {"routes_create": 1, "routes_create_specs": [{"source": "lfo_1", "destination": "filter_1_cutoff"}]},
        {"routes_update_specs": [{"slot": 2, "destination": "filter_1_cutoff", "amount": 0.5}]},
        {"routes_update_specs": [{"slot": 2, "source": "lfo_2", "amount": 0.5}]},
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.4.destination": "filter_2_cutoff"}},
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.4.source": "lfo_1"}},
        # the parent shapes: writing the table or a whole slot can create pairs too
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}": [{"source": "lfo_1", "destination": "d"}]}},
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.4": {"source": "lfo_1"}}},
        # a count without its subject is not decidable either
        {"routes_create": 2, "routes_create_specs": []},
        {"routes_update": 1, "routes_update_specs": []},
    ]
    for world in cases:
        findings = rule_findings(world)
        assert findings, world
        assert all(finding.detail["reason"] == "rule_not_evaluated" for finding in findings), world
        assert all(finding.detail["evaluated"] is False for finding in findings), world
        assert all(finding.informational is False for finding in findings), world
        assert all(finding.detail["guard"] == "route_pair_created" for finding in findings), world


def test_the_guard_stays_silent_where_no_pair_is_created():
    quiet = [
        {},  # nothing
        {"routes_remove": 1},  # a removal creates no pair
        {"routes_update_specs": [{"slot": 2, "amount": 0.25}]},  # amount-only
        {"routes_update_specs": [{"slot": 2, "bipolar": 1.0, "fields": {"power": 0.5}}]},
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.4.destination": ""}},  # clears
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.4.source": ""}},
        {"state_paths_changed": {f"{VC.MUTABLE_ROOT}.modulation_4_amount": 0.0}},  # amount field
        {"state_paths_changed": {"settings.filter_1_cutoff": 40.0}},  # unrelated
        {"controls_changed": {"filter_1_cutoff": 40.0}},
    ]
    for world in quiet:
        assert rule_findings(world) == [], world


def test_the_guard_fires_on_a_control_bound_into_the_route_table():
    """The parameter channel cannot be a bypass.

    A control write never appears in `state_paths_changed`, so a control whose
    declared binding addressed the route table would create a pair unseen by the
    state scan. No shipped Vital control does that, so this is the **decidability**
    case rather than a live finding: the guard reads the profile instead of
    assuming the channel is unreachable (found by independent audit, 2026-10-11,
    which could not rule the channel out from the registry alone).
    """
    bound = {
        "id": "routes_control",
        "binding": {"state_key": f"{VC.ROUTE_TABLE_KEY}.7.source"},
    }
    unbound = {"id": "tone_cutoff", "binding": {"state_key": "filter_1_cutoff"}}
    ruleset = RULES.dependencies({"controls": [bound, unbound]})

    fired = [
        finding
        for finding in ruleset.evaluate({"controls_changed": {"routes_control": 1.0}}, ruleset.rules)
        if finding.rule == "route_into_off_filter"
    ]
    assert fired, "a control bound into the route table must be refused"
    assert fired[0].informational is False
    assert fired[0].detail["guard"] == "route_pair_created"
    assert "routes_control" in json.dumps(fired[0].subject)

    quiet = [
        finding
        for finding in ruleset.evaluate({"controls_changed": {"tone_cutoff": 40.0}}, ruleset.rules)
        if finding.rule == "route_into_off_filter"
    ]
    assert quiet == [], "an unrelated control write stays available"


def test_the_rule_discloses_what_it_does_not_cover():
    """The guard's subject list is part of the decision, so it is asserted."""
    guard = RULES._guard_for("route_into_off_filter")
    assert guard["decision"] == "refuse_before_any_write"
    assert any("binding" in line for line in guard["subjects"])
    text = " ".join(guard["not_subject"])
    for kept in ("routes_remove", "state writes outside the route table"):
        assert kept in text, guard["not_subject"]
    assert guard["not_subject"], "a refusal must name what it does not cover"


# ---------------------------------------------------------------------------
# Gate 1 — through the engine, with the Vital provider and its real codec
# ---------------------------------------------------------------------------


def vital_transaction(request: dict, *, dry_run: bool):
    host = T7.vital_host()
    provider = T7.provider_on_fixtures()
    profile = provider.build_profile(dict(T7.IDENTITY))
    registry = T7.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T7.IDENTITY
    )
    result = T7.Transaction(
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
    return result, host


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "payload",
    [
        {"routes_create": [{"source": "lfo_1", "destination": "filter_1_cutoff", "amount": 0.0625}]},
        {"routes_update": [{"slot": 1, "destination": "filter_1_cutoff", "amount": 0.5}]},
        {"state": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.2.destination": "filter_1_cutoff"}},
        {"state": {"settings.reverb_on": 0.0}, "routes_create": [{"source": "lfo_1", "destination": "filter_1_cutoff", "amount": 0.05}]},
    ],
)
def test_creating_a_pair_is_refused_before_the_first_write(payload, dry_run):
    result, host = vital_transaction(payload, dry_run=dry_run)
    assert result.ok is False, result.data
    assert isinstance(result.error, DependencyViolation), result.error
    assert result.error.detail["unevaluable"] == ["route_into_off_filter"]
    assert all(row["evaluated"] is False for row in result.error.detail["findings"])
    assert result.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert result.data.get("would_write") is not True, "a preview must not call this applicable"
    assert [row for row in host.log if row[0].startswith("write")] == []


def test_removing_and_adjusting_a_route_are_still_available():
    """Non-vacuity: the gate is not a blanket ban on modulation work."""
    removed, host = vital_transaction({"routes_remove": [{"slot": 1}]}, dry_run=True)
    assert removed.ok is True, removed.error
    assert removed.data["would_write"] is True
    assert [row for row in host.log if row[0].startswith("write")] == []

    adjusted, host = vital_transaction({"routes_update": [{"slot": 1, "amount": 0.25}]}, dry_run=True)
    assert adjusted.ok is True, adjusted.error
    assert adjusted.data["would_write"] is True

    cleared, _host = vital_transaction(
        {"state": {f"{VC.MUTABLE_ROOT}.{VC.ROUTE_TABLE_KEY}.0.destination": ""}}, dry_run=True
    )
    assert cleared.ok is True, cleared.error

    unrelated, _host = vital_transaction({"state": {"settings.chorus_delay_1": -8.0}}, dry_run=True)
    assert unrelated.ok is True, unrelated.error


def test_a_committed_removal_still_writes_and_restores():
    """The gate must not break the operations it does not cover."""
    result, host = vital_transaction({"routes_remove": [{"slot": 1}]}, dry_run=False)
    assert result.ok is True, result.error
    assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
    assert result.outcome.host.write_calls_attempted == 1
    assert [label for label, _d in host.log if label == "write_state_slice"] == ["write_state_slice"]


# ---------------------------------------------------------------------------
# Gate 1 — through the registered MCP tools
# ---------------------------------------------------------------------------


def test_without_the_rule_table_the_write_tool_is_the_control(demo):
    """Control: the refusal comes from the rule table, not from the tool.

    The demo provider declares no route rule, so the *same* request succeeds here
    and refuses one test below. Without this case, a tool that refused every route
    create for an unrelated reason would look like a working gate.
    """
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": INSTANCE,
            "request": {"routes_create": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}]},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope


def test_the_registered_modulation_tool_refuses_a_create_with_the_vital_rule_table(demo):
    demo["factory"]["make"] = lambda: preload_provider()
    instance = {"track_index": 0, "fx_index": 1}

    for dry_run in (True, False):
        envelope = call(
            demo["mcp"],
            "plugin_modulation",
            {
                "instance": instance,
                "op": "create",
                "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}],
                "dry_run": dry_run,
            },
        )
        assert envelope["ok"] is False, (dry_run, envelope)
        assert envelope["error"]["code"] == "DependencyViolation", envelope["error"]
        assert envelope["error"]["detail"]["unevaluable"] == ["route_into_off_filter"]
        assert all(row["evaluated"] is False for row in envelope["error"]["detail"]["findings"])
        assert envelope["outcome"]["verdict"] == "REFUSED_BEFORE_ANY_WRITE"
        assert envelope["outcome"]["final_state"] == "unchanged_verified"
        assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
        assert envelope["outcome"]["host"]["write_effects_verified"] == 0
    assert write_verbs(demo["host"]) == [], "no write verb may be reached"
    assert json.dumps(envelope)


def test_the_registered_modulation_tool_still_lists_and_removes(demo):
    demo["factory"]["make"] = lambda: preload_provider()
    instance = {"track_index": 0, "fx_index": 1}
    listed = call(demo["mcp"], "plugin_modulation", {"instance": instance, "op": "list"})
    assert listed["ok"] is True, listed
    assert listed["data"]["routes"], "the demo document declares two routes"

    removed = call(
        demo["mcp"],
        "plugin_modulation",
        {"instance": instance, "op": "remove", "selector": {"slot": 1}, "dry_run": False},
    )
    assert removed["ok"] is True, removed
    assert removed["outcome"]["verdict"] == "ALL_VERIFIED"
    assert demo["host"].count("write_state_slice") == 1


def test_the_write_tool_refuses_a_route_create_under_the_vital_rule_table(demo):
    demo["factory"]["make"] = lambda: preload_provider()
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": INSTANCE,
            "request": {"routes_create": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}]},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "DependencyViolation"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert write_verbs(demo["host"]) == []


# ---------------------------------------------------------------------------
# Gate 2 — instance continuity
# ---------------------------------------------------------------------------


def test_a_token_carrying_mutation_is_refused_before_any_host_work(demo):
    issued = call(demo["mcp"], "plugin_instances", {})
    token = issued["meta"]["target_token"]
    facts = demo["host"].fx_facts(0, 1)

    # the measured L-3 shape: a **replacement** that reproduces every fact the host
    # exposes (same build, same handle, same block, same parameter store). The fixture
    # installs a fresh slot object and discards the old one - mutating the object in
    # place would let an earlier reference keep pointing at a live instance, which is
    # not the case being tested (raised by an independent audit, 2026-10-11).
    original_slot = demo["host"].slot(0, 1)
    replaced = demo["host"].external_swap_slot_object(
        track_index=0,
        fx_index=1,
        name=facts.fx_name,
        class_id=facts.class_id,
        session_handle=facts.session_handle,
    )
    assert replaced is not original_slot, "a new object must be installed"
    after = demo["host"].fx_facts(0, 1)
    assert RS.facts_identity(after) == RS.facts_identity(facts), "the replacement is indistinguishable"
    assert after.session_handle == facts.session_handle
    assert after.param_count == facts.param_count and after.fx_name == facts.fx_name

    instance = {"track_index": 0, "fx_index": 1, "target_token": token}
    host_reads_before = len(demo["host"].log)
    for dry_run in (True, False):
        envelope = call(
            demo["mcp"],
            "plugin_write",
            {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": dry_run},
        )
        assert envelope["ok"] is False, (dry_run, envelope)
        assert envelope["error"]["code"] == "IdentityContinuityUnprovable"
        detail = envelope["error"]["detail"]
        assert detail["reason"] == "instance_continuity_not_provable"
        assert detail["reads_unaffected"] is True
        assert "location" in detail["not_provable_by"]
        assert detail["measured_by"].endswith("p8_l3_identity__20261010T203101Z__dcd339e9c219.json")
        assert "remedy" in detail
    assert len(demo["host"].log) == host_reads_before, "the refusal precedes every host call"
    assert write_verbs(demo["host"]) == []


def test_the_same_location_addressed_write_is_available_and_honest(demo):
    """Non-vacuity: the gate refuses the *claim*, not the operation.

    After the identical replacement, a location-addressed write proceeds - its
    safety is established inside the call (capture, revalidate, write, verify,
    restore) and does not depend on cross-call continuity. The response states
    exactly that, and makes no continuity claim.
    """
    facts = demo["host"].fx_facts(0, 1)
    demo["host"].external_swap_slot_object(
        track_index=0, fx_index=1, name=facts.fx_name, class_id=facts.class_id,
        session_handle=facts.session_handle,
    )
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": INSTANCE, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": False},
    )
    assert envelope["ok"] is True, envelope
    assert envelope["outcome"]["verdict"] == "ALL_VERIFIED"
    assert envelope["outcome"]["host"]["write_calls_attempted"] >= 1
    assert envelope["meta"]["identity_basis"]["not_claimed"] == ["instance_continuity"]
    assert "identity" not in json.dumps(envelope["outcome"])


def test_the_read_paths_keep_accepting_the_token(demo):
    issued = call(demo["mcp"], "plugin_instances", {})
    token = issued["meta"]["target_token"]
    instance = {"track_index": 0, "fx_index": 1, "target_token": token}
    for tool, arguments in (
        ("plugin_describe", {}),
        ("plugin_read", {"controls": ["tone_cutoff"]}),
        ("plugin_state", {}),
        ("plugin_modulation", {"op": "list"}),
        ("plugin_modulation", {"op": "depth", "destination": "tone_level", "depth": 12}),
    ):
        envelope = call(demo["mcp"], tool, {"instance": instance, **arguments})
        assert envelope["ok"] is True, (tool, envelope)


def test_the_engine_keeps_its_own_token_the_refusal_is_at_the_boundary():
    """The engine is token-scoped by design; the refusal is a boundary policy.

    A `Transaction` is *given* a token by its caller and revalidates it against
    fresh facts before every unit - that is the in-call mechanism. What is refused
    is a **caller-supplied** token coming from an earlier call, which the api layer
    decides before the bridge is touched (`test_a_token_carrying_mutation_is_refused_before_any_host_work`).
    """
    host = build_host()
    provider = MockProvider()
    facts = host.fx_facts(0, 1)
    registry = RS.TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    from reaper_reapy_mcp.plugins import transaction as TX

    transaction = TX.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=M.PluginInstanceRef(
            host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
        ),
        request={"controls": {"tone_cutoff": 24.0}},
        token=token,
        registry=registry,
        dry_run=False,
    )
    # the engine itself is token-scoped and therefore fine; what is refused is a
    # **caller-supplied** token, which the api layer decides before the bridge is
    # touched (asserted here as the contract, not as a new engine rule)
    result = transaction.run()
    assert result.ok is True, result.error
    assert write_verbs(host) != []


# ---------------------------------------------------------------------------
# The disposition matrix: what a caller may do, with the counters as evidence
# ---------------------------------------------------------------------------

VITAL_RULE_INSTANCE = {"track_index": 0, "fx_index": 1}

ALLOWED = "allowed"
REFUSED = "refused"


@pytest.mark.parametrize(
    "tool,arguments,disposition",
    [
        # read-only capabilities
        ("plugin_instances", {}, ALLOWED),
        ("plugin_describe", {"instance": VITAL_RULE_INSTANCE}, ALLOWED),
        ("plugin_find", {"instance": VITAL_RULE_INSTANCE, "query": "tone"}, ALLOWED),
        ("plugin_read", {"instance": VITAL_RULE_INSTANCE, "controls": ["tone_cutoff"]}, ALLOWED),
        ("plugin_state", {"instance": VITAL_RULE_INSTANCE}, ALLOWED),
        ("plugin_modulation", {"instance": VITAL_RULE_INSTANCE, "op": "list"}, ALLOWED),
        (
            "plugin_modulation",
            {"instance": VITAL_RULE_INSTANCE, "op": "depth", "destination": "tone_level", "depth": 12},
            ALLOWED,
        ),
        # mutations whose safety does not depend on the route rule
        (
            "plugin_write",
            {"instance": VITAL_RULE_INSTANCE, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": False},
            ALLOWED,
        ),
        (
            "plugin_write",
            {"instance": VITAL_RULE_INSTANCE, "request": {"state": {"settings.mode": "mono"}}, "dry_run": True},
            ALLOWED,
        ),
        (
            "plugin_write",
            {"instance": VITAL_RULE_INSTANCE, "request": {"routes_remove": [{"slot": 2}]}, "dry_run": True},
            ALLOWED,
        ),
        (
            "plugin_modulation",
            {"instance": VITAL_RULE_INSTANCE, "op": "update", "selector": {"slot": 2}, "routes": [{"amount": 0.25}]},
            ALLOWED,
        ),
        # the route gate
        (
            "plugin_write",
            {"instance": VITAL_RULE_INSTANCE, "request": {"routes_create": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}]}},
            REFUSED,
        ),
        (
            "plugin_write",
            {"instance": VITAL_RULE_INSTANCE, "request": {"routes_create": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}]}, "dry_run": False},
            REFUSED,
        ),
        (
            "plugin_modulation",
            {"instance": VITAL_RULE_INSTANCE, "op": "create", "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}]},
            REFUSED,
        ),
        (
            "plugin_modulation",
            {"instance": VITAL_RULE_INSTANCE, "op": "create", "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}], "dry_run": False},
            REFUSED,
        ),
    ],
)
def test_the_disposition_matrix_holds_with_the_counters_as_evidence(
    demo, tool, arguments, disposition
):
    """One table for the whole surface: what is available, and the proof.

    A refusal is only proven by the **counters and the call log**, not by the
    status: every refused case must show `write_calls_attempted == 0` and no write
    verb in the host's log, and every allowed case must not contradict its own
    stated claim (a dry run writes nothing by design; a committed mutation is
    verified).
    """
    demo["factory"]["make"] = lambda: preload_provider()
    envelope = call(demo["mcp"], tool, arguments)
    writes = write_verbs(demo["host"])
    dry_run = arguments.get("dry_run", True)

    if disposition == REFUSED:
        assert envelope["ok"] is False, (tool, arguments, envelope)
        outcome = envelope.get("outcome") or {}
        assert outcome.get("host", {}).get("write_calls_attempted", 0) == 0, outcome
        assert outcome.get("host", {}).get("write_effects_unknown", 0) == 0, outcome
        assert writes == [], (tool, arguments, writes)
        assert envelope["error"]["code"] == "DependencyViolation", envelope["error"]
        assert "route_into_off_filter" in envelope["error"]["detail"]["unevaluable"]
    else:
        assert envelope["ok"] is True, (tool, arguments, envelope)
        if tool == "plugin_write" and dry_run is False:
            assert envelope["outcome"]["verdict"] == "ALL_VERIFIED", envelope["outcome"]
            assert writes != [], "a committed write must have reached the host"
        else:
            assert writes == [], (tool, arguments, writes)
            if envelope.get("outcome"):
                assert envelope["outcome"]["host"]["write_calls_attempted"] == 0, envelope["outcome"]
