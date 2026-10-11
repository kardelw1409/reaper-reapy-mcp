"""P2: the transaction engine proven on mocks.

The matrix covers preflight refusals with `write_calls_attempted == 0`,
snapshot-or-refuse (B-32), document-before-parameters order, per-channel
verified rollback (B-33..B-37), external-change handling (B-41..B-43, B-53),
the four honest rollback verdicts, cancellation (B-51/B-52), the counter
invariant, the dry-run default and the early tool-registration path (AUD-011).
"""

from __future__ import annotations

import threading

import pytest

import fixtures as F
import mock_host as MH
from mock_codec_b import CodecB, b_document, make_b_block_text
from mock_provider import DemoCodec, MockProvider

from reaper_reapy_mcp.plugins import tools as plugin_tools
from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins.errors import (
    DependencyViolation,
    InvalidValue,
    PlanError,
    RollbackFailed,
    StateConflict,
    UnsupportedControl,
    WriteNotApplied,
    Wedged,
)
from reaper_reapy_mcp.plugins.model import (
    FinalState,
    PluginInstanceRef,
    ResolvedRequest,
    Verdict,
)
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry
from reaper_reapy_mcp.plugins.transaction import Transaction

MODE_REQUEST = {"state": {"settings.mode": "mono"}, "routes_remove": [{"slot": 1}, {"slot": 2}]}
BOTH_CHANNELS = {**MODE_REQUEST, "controls": {"tone_cutoff": 24.0}}


def build_host(*, sleeper=None, reset_params=None) -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    target = MH.MockFxSlot()
    return MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [foreign, target])],
        sleeper=sleeper,
        reset_params_on_state_write=reset_params,
    )


def make(
    *,
    request,
    host=None,
    dry_run=True,
    cancel=None,
    provider=None,
    codec=None,
    fx_index=1,
):
    host = host if host is not None else build_host()
    provider = provider or MockProvider()
    facts = host.fx_facts(0, fx_index)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": fx_index},
        provider="demo",
        identity=provider.identify(facts),
    )
    transaction = Transaction(
        provider=provider,
        codec=codec or provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request=request,
        token=token,
        registry=registry,
        dry_run=dry_run,
        cancel=cancel,
    )
    return transaction, host


def assert_counters_consistent(result):
    accounting = result.outcome.host
    assert accounting.write_calls_attempted == (
        accounting.write_effects_verified + accounting.write_effects_unknown
    ), accounting.as_dict()
    assert result.outcome.mapping_is_consistent()


def labels(host: MH.MockHostBridge) -> list[str]:
    return [label for label, _detail in host.log if label.startswith("write")]


# ---------------------------------------------------------------------------
# Dry-run default (P2-5, B-49 groundwork)
# ---------------------------------------------------------------------------


def test_dry_run_default_applies_nothing():
    transaction, host = make(request=BOTH_CHANNELS)  # dry_run defaults to True
    before_block = host.slot(0, 1).block
    before_params = dict(host.slot(0, 1).params)
    result = transaction.run()
    assert result.ok and not result.committed
    assert result.outcome.verdict is Verdict.NO_WRITES
    assert result.outcome.final_state is FinalState.UNCHANGED_VERIFIED
    assert result.outcome.host.zero_writes
    assert result.data["dry_run"] is True and result.data["would_write"] is True
    assert result.data["plan_summary"]["n_routes_remove"] == 2
    assert host.slot(0, 1).block == before_block
    assert host.slot(0, 1).params == before_params
    assert_counters_consistent(result)


@pytest.mark.parametrize("value", [0, 1, "", "false", [], {}, None])
def test_a_non_boolean_dry_run_is_refused_before_any_host_work(value):
    """`dry_run` is a real boolean or a refusal - never a truthiness test.

    A client that sends ``0`` or ``""`` is saying "do not apply"; ``bool()``
    would have read that as "apply for real", and ``"false"`` is the classic
    falsy-looking string that would have **opted in**. Found by an independent
    read-only audit after P10 (the transport declares these parameters `Any`).
    """
    host = build_host()
    with pytest.raises(InvalidValue) as excinfo:
        make(request=BOTH_CHANNELS, host=host, dry_run=value)
    assert excinfo.value.detail["argument"] == "dry_run"
    assert excinfo.value.detail["value"] == value
    # the harness itself reads the instance facts to mint the target token; the
    # refusal must happen before anything else touches the host, and no write
    # verb may be reached at all
    assert [label for label, _detail in host.log] == ["fx_facts"]
    assert labels(host) == []


def test_the_engine_refuses_a_parameter_the_provider_forgot_to_bind():
    """Defence in depth (`_require_bound_parameters`), previously unexercised.

    A provider whose normalization does not refuse an unbound control would let
    the plan claim a change the batched write silently omits. The engine refuses
    it for **any** provider, before the first write.
    """

    class LaxProvider(MockProvider):
        def normalize_request(self, profile, request):
            resolved = super().normalize_request(profile, request)
            for row in resolved.controls.values():
                if row.get("channel") == "parameter":
                    (row.get("binding") or {}).pop("ident", None)
            return resolved

    host = build_host()
    transaction, _host = make(
        request={"controls": {"tone_cutoff": 24.0}}, host=host, dry_run=False, provider=LaxProvider()
    )
    result = transaction.run()
    assert result.ok is False
    assert result.error is not None and result.error.code == "UnsupportedControl"
    assert result.error.detail["reason"] == "ident_not_bound"
    assert result.error.detail["control"] == "tone_cutoff"
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert labels(host) == []
    assert_counters_consistent(result)


# ---------------------------------------------------------------------------
# Refusals before any write
# ---------------------------------------------------------------------------


def test_unknown_control_is_refused_with_zero_writes():
    transaction, host = make(request={"controls": {"no_such_control": 1}}, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, UnsupportedControl)
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.final_state is FinalState.UNCHANGED_VERIFIED
    assert result.outcome.host.write_calls_attempted == 0
    assert result.error.detail["problems"][0]["detail"]["suggestions"] == []
    assert host.count("write_params") == 0 and host.count("write_state_slice") == 0
    assert_counters_consistent(result)


def test_percent_unit_trap_is_refused_before_any_write():
    transaction, host = make(request={"controls": {"mix_percent": 1.0}}, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, InvalidValue)
    assert result.error.detail["problems"][0]["detail"]["trap"] is True
    assert result.outcome.host.write_calls_attempted == 0


def test_unrooted_state_path_is_refused_before_any_write():
    transaction, host = make(request={"state": {"nested.path": 1}}, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, InvalidValue)
    assert result.outcome.host.write_calls_attempted == 0


def test_hard_dependency_refuses_before_any_write():
    transaction, host = make(request={"state": {"settings.mode": "mono"}}, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, DependencyViolation)
    assert result.error.detail["findings"][0]["rule"] == "mode_change_with_active_routes"
    assert result.outcome.host.write_calls_attempted == 0
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert_counters_consistent(result)


def test_projection_allows_the_same_change_when_routes_are_removed_together():
    """The projected world (routes removed) satisfies the rule the raw
    snapshot alone would falsely refuse."""
    transaction, host = make(request=MODE_REQUEST, dry_run=False)
    result = transaction.run()
    assert result.ok and result.committed, result.error
    doc = F.decode_block_document(host.slot(0, 1).block)
    assert doc["settings"]["mode"] == "mono"
    assert all(not route.get("destination") for route in doc["routes"])


# ---------------------------------------------------------------------------
# Snapshot-or-refuse (B-32)
# ---------------------------------------------------------------------------


def test_capture_failure_refuses_with_zero_writes():
    host = build_host()
    host.inject("read_params", "store-nothing")  # the full vector read comes back short
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.error.code == "SnapshotUnavailable"
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_state_slice") == 0
    assert_counters_consistent(result)


def test_unreadable_parameter_ident_in_the_vector_refuses():
    host = build_host()
    # index 7 carries no declared control, so the live **binding** check passes and
    # the refusal comes from the full-vector capture itself (the control indices
    # 3..6 are covered by their own binding tests in test_p8_binding.py)
    host.slot(0, 1).missing_idents.add("7:1007")
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.error.code == "SnapshotUnavailable"
    assert result.error.detail["missing"] == [7]
    assert result.outcome.host.write_calls_attempted == 0


# ---------------------------------------------------------------------------
# Order and multi-unit sequencing
# ---------------------------------------------------------------------------


def test_document_before_parameters_order_is_enforced_on_the_call_log():
    transaction, host = make(request=BOTH_CHANNELS, dry_run=False)
    result = transaction.run()
    assert result.ok, result.error
    order = labels(host)
    assert order[-1] == "write_params"
    assert all(label == "write_state_slice" for label in order[:-1])
    assert order[:-1].count("write_state_slice") == 3  # 1 path unit + 2 route units
    assert_counters_consistent(result)


def test_each_route_is_one_state_write_and_revisions_do_not_self_conflict():
    transaction, host = make(request=MODE_REQUEST, dry_run=False)
    result = transaction.run()
    assert result.ok, result.error
    assert labels(host) == ["write_state_slice"] * 3
    assert result.outcome.verdict is Verdict.ALL_VERIFIED
    assert result.outcome.final_state is FinalState.COMMITTED
    assert_counters_consistent(result)


def test_parameter_only_write_is_one_batched_call():
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0, "tone_level": 0.2}}, dry_run=False)
    result = transaction.run()
    assert result.ok, result.error
    assert labels(host) == ["write_params"]
    assert host.count("write_params") == 1
    assert host.slot(0, 1).params["3:1003"] == 0.24
    assert all(row["agrees"] for row in result.outcome.channels["parameters"].rows)
    assert_counters_consistent(result)


# ---------------------------------------------------------------------------
# WriteNotApplied / rollback (B-14, B-33, B-37, B-36)
# ---------------------------------------------------------------------------


def test_state_write_that_does_not_land_is_write_not_applied_and_restored():
    host = build_host()
    before_block = host.slot(0, 1).block
    before_params = dict(host.slot(0, 1).params)
    host.inject("write_state_slice", "store-nothing")
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, WriteNotApplied)
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert result.outcome.final_state is FinalState.UNCHANGED_VERIFIED
    assert result.outcome.rollback.attempted and result.outcome.rollback.verified
    assert result.outcome.rollback.channels == {"state": True, "parameters": True}
    assert host.slot(0, 1).block == before_block
    assert host.slot(0, 1).params == before_params
    assert result.outcome.host.write_calls_attempted == 3  # failed unit + 2 restores
    assert_counters_consistent(result)


def test_failure_after_verified_state_units_restores_both_channels():
    host = build_host()
    before_block = host.slot(0, 1).block
    before_params = dict(host.slot(0, 1).params)
    host.inject("write_params", "wedge")
    transaction, host = make(request=BOTH_CHANNELS, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, Wedged)
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert result.outcome.final_state is FinalState.UNCHANGED_VERIFIED
    assert result.outcome.rollback.verified
    assert host.slot(0, 1).block == before_block
    assert host.slot(0, 1).params == before_params
    # the three forward state units were verified; the wedged parameter write is unknown
    assert result.outcome.host.write_effects_unknown == 1
    assert_counters_consistent(result)


def test_document_reset_restores_parameters_after_the_document():
    """B-37: a document write resets ordinary parameters, so the rollback must
    rewrite them AFTER the document restore and verify both."""
    reset_value = 0.54688
    host = build_host(reset_params={"3:1003": reset_value})
    before_params = dict(host.slot(0, 1).params)
    host.inject("write_params", "store-nothing")  # force a failure after the state units
    transaction, host = make(request=BOTH_CHANNELS, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert host.slot(0, 1).params == before_params  # parameters restored after the document
    restore_labels = labels(host)
    assert restore_labels[-1] == "write_params"
    reset_rows = [row for row in result.outcome.channels["parameters"].rows if row.get("restore")]
    assert reset_rows and all(row["agrees"] for row in reset_rows)
    assert_counters_consistent(result)


def test_rollback_failure_is_loud_and_unknown():
    host = build_host()
    host.inject("write_state_slice", "raise", times=2)
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, RollbackFailed)
    assert result.outcome.verdict is Verdict.ROLLBACK_FAILED
    assert result.outcome.final_state is FinalState.UNKNOWN
    assert not result.outcome.rollback.verified
    assert result.error.detail["cause"]["code"] == "Wedged"
    assert any("state" == gap["channel"] for gap in result.outcome.named_gaps)
    assert_counters_consistent(result)


def test_restore_write_that_lands_wrong_is_loud_and_never_claims_restored():
    """B-35 shape: a restore read-back mismatch is reported truthfully."""
    host = build_host()
    host.inject("write_params", "store-nothing")  # the forward parameter write fails
    host.inject("write_params", "store-wrong")  # the parameter RESTORE lands wrong
    transaction, host = make(request=BOTH_CHANNELS, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.outcome.verdict is Verdict.ROLLBACK_FAILED
    assert result.outcome.final_state is FinalState.UNKNOWN
    assert not result.outcome.rollback.verified
    assert result.outcome.rollback.channels["parameters"] is False
    assert result.error.code == "RollbackFailed"
    assert_counters_consistent(result)


def test_failed_and_not_restored_when_both_channels_diverged_externally():
    host = build_host()
    host.inject("write_params", "drop-after-return")
    host.after(
        "write_params",
        1,
        lambda: (
            host.external_set_document({**F.demo_document(), "version": 9}, fx_index=1),
            host.external_set_param("7:1007", 0.42, fx_index=1),
        ),
    )
    transaction, host = make(request=BOTH_CHANNELS, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.outcome.verdict is Verdict.FAILED_AND_NOT_RESTORED
    assert result.outcome.final_state is FinalState.UNKNOWN
    assert result.outcome.rollback.channels == {"state": False, "parameters": False}
    external_gaps = [gap for gap in result.outcome.named_gaps if gap["reason"] == "not_restored (external_change)"]
    assert {gap["channel"] for gap in external_gaps} == {"state", "parameters"}
    # foreign state is never overwritten by a rollback
    assert F.decode_block_document(host.slot(0, 1).block)["version"] == 9
    assert host.slot(0, 1).params["7:1007"] == 0.42
    assert_counters_consistent(result)


def test_concurrent_transactions_on_one_target_are_serialized():
    """B-50: the target-scoped lock serializes two operations on one target."""
    host = build_host()
    provider = MockProvider()
    registry = TargetTokenRegistry()
    facts = host.fx_facts(0, 1)

    def build() -> Transaction:
        return Transaction(
            provider=provider,
            codec=provider.codec(),
            bridge=host,
            profile=provider.build_profile(provider.identify(facts)),
            ref=PluginInstanceRef(
                host="reaper",
                location={"track_index": 0, "fx_index": 1},
                provider="demo",
                identity=provider.identify(facts),
            ),
            request={"controls": {"tone_cutoff": 24.0}},
            token=registry.issue(facts=facts),
            registry=registry,
            dry_run=False,
        )

    token = registry.issue(facts=facts)
    assert registry.issue(facts=facts) is token  # one token per validated target
    lock = registry.lock_for(token.id)
    results: dict[str, object] = {}
    lock.acquire()
    try:
        worker = threading.Thread(target=lambda: results.update(run=build().run()))
        worker.start()
        worker.join(timeout=0.4)
        blocked = worker.is_alive()
        writes_while_blocked = host.count("write_params")
    finally:
        lock.release()
    worker.join(timeout=5)
    assert blocked, "a second transaction on the same target must block on the target lock"
    assert writes_while_blocked == 0
    assert results["run"].ok
    assert host.count("write_params") == 1


def test_local_and_unverifiable_writes_are_never_reported_as_unknown():
    host = build_host()
    host.inject("write_state_slice", "store-nothing")
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert result.outcome.host.write_effects_unknown == 0
    assert result.outcome.host.write_effects_verified == result.outcome.host.write_calls_attempted


# ---------------------------------------------------------------------------
# External changes (B-41..B-43, B-53)
# ---------------------------------------------------------------------------


def test_external_change_before_a_unit_refuses_with_zero_writes():
    host = build_host()
    # the second parameter read is the unit's own revalidation: the external
    # value is already visible there while the expected revision was seeded
    # from the snapshot (occurrence 1)
    host.after("read_params", 2, lambda: host.external_set_param("7:1007", 0.42, fx_index=1))
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, StateConflict)
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert result.error.detail["expected"] != result.error.detail["observed"]
    assert_counters_consistent(result)


def test_externally_diverged_channel_is_left_untouched_during_rollback():
    """B-53: a channel that no longer matches the transaction's own last
    verified state or its own attempted write is foreign state - untouched."""
    host = build_host()
    host.inject("write_params", "drop-after-return")  # the write lands, the outcome is lost
    host.after("write_params", 1, lambda: host.external_set_param("7:1007", 0.42, fx_index=1))
    before_block = host.slot(0, 1).block
    transaction, host = make(request=BOTH_CHANNELS, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.outcome.verdict is Verdict.FAILED_AND_PARTIALLY_RESTORED
    assert result.outcome.final_state is FinalState.PARTIALLY_RESTORED
    assert result.outcome.rollback.channels == {"state": True, "parameters": False}
    assert not result.outcome.rollback.verified
    assert any(gap["reason"] == "not_restored (external_change)" for gap in result.outcome.named_gaps)
    assert host.slot(0, 1).block == before_block  # the state channel was restored
    assert host.slot(0, 1).params["7:1007"] == 0.42  # the foreign value was NOT overwritten
    assert_counters_consistent(result)


def test_stale_token_refuses_before_any_write():
    host = build_host()
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    host.external_replace_slot(class_id="00000000000000000000000000000001", fx_index=1)
    result = transaction.run()
    assert not result.ok and result.error.code == "TargetChanged"
    assert result.outcome.host.write_calls_attempted == 0
    assert result.error.detail["fields"] == ["class_id"]


# ---------------------------------------------------------------------------
# Cancellation (B-51/B-52)
# ---------------------------------------------------------------------------


def test_cancellation_before_any_write_refuses():
    transaction, host = make(request=MODE_REQUEST, dry_run=False, cancel=lambda: True)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, PlanError)
    assert result.error.detail["cancelled"] is True
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0


def test_cancellation_during_a_state_write_reports_truthfully_and_restores():
    flag = {"cancelled": False}

    def flip(_seconds: float) -> None:
        flag["cancelled"] = True

    host = build_host(sleeper=flip)
    before_block = host.slot(0, 1).block
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False, cancel=lambda: flag["cancelled"])
    result = transaction.run()
    assert not result.ok and isinstance(result.error, PlanError)
    assert result.outcome.host.write_effects_unknown == 1  # the in-flight unit is unknowable
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert host.slot(0, 1).block == before_block  # rollback restored what could be established
    assert_counters_consistent(result)


def test_cancellation_during_a_parameter_write_reports_truthfully():
    flag = {"cancelled": False}
    host = build_host()
    host.after("write_params", 1, lambda: flag.update(cancelled=True))
    transaction, host = make(
        request=BOTH_CHANNELS, host=host, dry_run=False, cancel=lambda: flag["cancelled"]
    )
    result = transaction.run()
    assert not result.ok
    assert result.outcome.host.write_effects_unknown == 1
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert_counters_consistent(result)


# ---------------------------------------------------------------------------
# Cross-codec genericity (AUD-004)
# ---------------------------------------------------------------------------


class ProviderB(MockProvider):
    """The same provider semantics over codec B's document shape."""

    def __init__(self) -> None:
        super().__init__()
        self._codec = CodecB()

    def normalize_request(self, profile, request):  # noqa: ANN001
        resolved = ResolvedRequest()
        for path, value in dict(request.get("state") or {}).items():
            resolved.state[str(path)] = value
        return resolved

    def codec(self) -> CodecB:
        return self._codec


def build_codec_b_host() -> MH.MockHostBridge:
    slot = MH.MockFxSlot(
        name="VST3b: Demo B",
        module="demo_b.vst3",
        class_id="FFEEDDCCBBAA99887766554433221100",
        block=make_b_block_text(F.flat_paths(b_document())),
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [slot])])


def test_engine_is_generic_over_a_different_codec_shape():
    host = build_codec_b_host()
    provider = ProviderB()
    transaction, host = make(
        request={"state": {"tree.engine.gain": 0.4}}, host=host, provider=provider, dry_run=False, fx_index=0
    )
    result = transaction.run()
    assert result.ok, result.error
    assert labels(host) == ["write_state_slice"]
    assert F.flat_paths(CodecB().decode(host.locate_state_slice(
        transaction.ref, host.fx_facts(0, 0), host.read_state_chunk(transaction.ref)
    )))["tree.engine.gain"] == 0.4
    assert_counters_consistent(result)


def test_engine_sequences_two_sections_as_two_units_with_codec_b():
    host = build_codec_b_host()
    provider = ProviderB()
    transaction, host = make(
        request={"state": {"tree.engine.gain": 0.4, "flags.bypass": True}},
        host=host,
        provider=provider,
        dry_run=False,
        fx_index=0,
    )
    result = transaction.run()
    assert result.ok, result.error
    assert labels(host) == ["write_state_slice", "write_state_slice"]
    flat = F.flat_paths(
        CodecB().decode(
            host.locate_state_slice(
                transaction.ref, host.fx_facts(0, 0), host.read_state_chunk(transaction.ref)
            )
        )
    )
    assert flat["flags.bypass"] is True and flat["tree.engine.gain"] == 0.4
    assert_counters_consistent(result)


# ---------------------------------------------------------------------------
# Early registration/dispatch path against mocks (AUD-011)
# ---------------------------------------------------------------------------


class _RecordingMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str):
        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


def test_registered_write_tool_keeps_the_dry_run_default_end_to_end():
    """The dispatch path defaults to dry-run, and the dry-run path performs
    zero writes through a real engine + mock host."""
    host = build_host()
    provider = MockProvider()

    def body(*, config, instance, request, dry_run=True, **kwargs):
        transaction, _ = make(
            request=request, host=host, provider=provider, dry_run=dry_run, fx_index=1
        )
        result = transaction.run()
        return plugin_tools.ok_envelope(
            "plugin_write",
            {"committed": result.committed, "dry_run": dry_run, **{"outcome": result.outcome.to_dict()}},
        )

    plugin_tools.register_dispatch("plugin_write", body)
    try:
        mcp = _RecordingMcp()
        plugin_tools.setup_plugin_tools(mcp, config=None)
        # re-injected AFTER registration: `setup_plugin_tools` attaches the P9
        # production bodies, and this test exercises the closure with its own body
        plugin_tools.register_dispatch("plugin_write", body)
        envelope = mcp.tools["plugin_write"](instance={"location": {"track_index": 0, "fx_index": 1}}, request=BOTH_CHANNELS)
        assert envelope["ok"] is True
        assert envelope["data"]["dry_run"] is True
        assert envelope["data"]["committed"] is False
        assert envelope["data"]["outcome"]["verdict"] == "NO_WRITES"
        assert host.count("write_state_slice") == 0 and host.count("write_params") == 0
    finally:
        plugin_tools.DISPATCH.pop("plugin_write", None)
        A.register_production_bodies()


# ---------------------------------------------------------------------------
# Review follow-ups (independent review, 2026-10-10)
# ---------------------------------------------------------------------------


def test_pre_write_transport_failure_is_a_refusal_with_zero_writes():
    """F8: a typed transport failure before any write refuses, never rolls back."""
    host = build_host()
    host.inject("read_state_chunk", "wedge")
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, Wedged)
    assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert host.count("write_state_slice") == 0
    assert_counters_consistent(result)


def test_forward_parameter_readback_mismatch_rolls_back():
    """F5 forward arm: a parameter write that lands wrong is ReadbackMismatch."""
    host = build_host()
    before_block = host.slot(0, 1).block
    before_params = dict(host.slot(0, 1).params)
    host.inject("write_params", "store-wrong")
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0}}, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.error.code in ("ReadbackMismatch", "WriteNotApplied")
    assert result.outcome.verdict in (
        Verdict.FAILED_AND_RESTORED,
        Verdict.FAILED_AND_PARTIALLY_RESTORED,
    )
    assert host.slot(0, 1).block == before_block
    assert result.outcome.host.write_effects_unknown == 0  # the mismatch is established
    assert_counters_consistent(result)


def test_revision_conflict_mid_sequence_rolls_back_the_verified_units():
    """B-42's second arm: the conflict arrives at unit >= 2, after a unit verified.

    The externally changed parameter channel is foreign state: the state channel
    is compensated, the parameter channel is left untouched and reported.
    """
    host = build_host()
    before_block = host.slot(0, 1).block
    # read_params occurrence 4 is unit 1's revalidation read (1 = snapshot,
    # 2 = unit 0 revalidation, 3 = unit 0 post-write vector)
    host.after("read_params", 4, lambda: host.external_set_param("7:1007", 0.42, fx_index=1))
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, StateConflict)
    assert result.outcome.verdict is Verdict.FAILED_AND_PARTIALLY_RESTORED
    assert result.outcome.final_state is FinalState.PARTIALLY_RESTORED
    assert result.outcome.stage == "state_write"
    assert result.outcome.rollback.channels == {"state": True, "parameters": False}
    assert host.slot(0, 1).block == before_block  # unit 0's write was compensated
    assert host.slot(0, 1).params["7:1007"] == 0.42  # the foreign value is untouched
    assert result.outcome.host.write_calls_attempted == 2  # unit 0 + the state restore
    assert_counters_consistent(result)


def test_a_read_failure_after_a_write_keeps_the_counters_honest():
    """The unverified write stays *unknown* even when the verify read dies.

    read_state_chunk occurrence 4 is the post-write read-back of unit 0.
    """
    host = build_host()
    host.inject_at("read_state_chunk", 4, "raise", error=Wedged("verify read died"))
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok and isinstance(result.error, Wedged)
    assert result.outcome.host.write_effects_unknown >= 1
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert_counters_consistent(result)


def test_writing_one_of_two_identical_instances_leaves_the_other_byte_identical():
    """B-28's write half: the addressed instance changes, its twin does not."""
    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [MH.MockFxSlot(), MH.MockFxSlot()])])
    twin_before = host.slot(0, 0).block
    transaction, host = make(request=MODE_REQUEST, host=host, dry_run=False, fx_index=1)
    result = transaction.run()
    assert result.ok, result.error
    assert host.slot(0, 0).block == twin_before
    assert F.decode_block_document(host.slot(0, 1).block)["settings"]["mode"] == "mono"
    assert_counters_consistent(result)


def test_a_rollback_names_a_plugin_derived_leaf_it_cannot_restore():
    """A codec may declare captured leaves the plugin re-derives (P8, Vital).

    The restore must not ask for them (the write could not land), and the outcome
    must name them: an outcome that says "restored" says which captured leaves were
    excluded and why, instead of quietly restoring fewer.

    Decision 2/5 (2026-10-10): the report separates the two claims. ``verified``
    means every channel the restore wrote was compared and matched; ``complete``
    means that set covered the whole capture. A deliberately excluded leaf leaves
    the restore *verified but not complete*, so the verdict is
    `FAILED_AND_PARTIALLY_RESTORED` - `FAILED_AND_RESTORED` would assert
    `unchanged_verified` for state that is not unchanged.
    """

    class DerivedCodec(DemoCodec):
        def restore_changes(self, doc, snap):
            changes = super().restore_changes(doc, snap)
            changes["derived_excluded"] = ["settings.mode"]
            return changes

    host = build_host()
    host.inject("write_params", "store-nothing", times=1)  # the batch fails to land
    transaction, host = make(
        request=BOTH_CHANNELS, host=host, dry_run=False, codec=DerivedCodec()
    )
    result = transaction.run()
    assert not result.ok
    assert result.outcome.rollback.verified, result.outcome.to_dict()
    gap = [row for row in result.outcome.named_gaps if row["reason"] == "derived_leaf_not_restored"]
    assert gap == [
        {
            "channel": "state",
            "reason": "derived_leaf_not_restored",
            "path": "settings.mode",
            "detail": "the provider declares this leaf as plugin-derived; a document write cannot set it",
        }
    ], result.outcome.named_gaps
    assert result.outcome.rollback.complete is False
    assert result.outcome.rollback.excluded == ["settings.mode"]
    assert result.outcome.verdict is Verdict.FAILED_AND_PARTIALLY_RESTORED
    assert result.outcome.final_state is FinalState.PARTIALLY_RESTORED
    # the serialized outcome keeps the same pair, so the gap cannot be lost at a boundary
    payload = result.outcome.to_dict()
    assert payload["rollback"]["complete"] is False
    assert payload["rollback"]["excluded"] == ["settings.mode"]
    assert payload["named_gaps"] == gap
    assert_counters_consistent(result)


def test_a_rollback_without_a_derived_leaf_is_verified_and_complete():
    """The positive control for the test above: no exclusion ⇒ full restore."""
    host = build_host()
    host.inject("write_params", "store-nothing", times=1)
    transaction, host = make(request=BOTH_CHANNELS, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert result.outcome.rollback.verified is True
    assert result.outcome.rollback.complete is True
    assert result.outcome.rollback.excluded == []
    assert result.outcome.named_gaps == []
    assert result.outcome.verdict is Verdict.FAILED_AND_RESTORED
    assert result.outcome.final_state is FinalState.UNCHANGED_VERIFIED


def test_a_derived_leaf_gap_survives_a_failed_restore_too():
    """The gap is a property of the capture, not of the verdict.

    Here the restore's own write fails as well, so no channel is verified back -
    and the exclusion is still named, because it is still true.
    """

    class DerivedCodec(DemoCodec):
        def restore_changes(self, doc, snap):
            changes = super().restore_changes(doc, snap)
            changes["derived_excluded"] = ["settings.mode"]
            return changes

    host = build_host()
    host.inject("write_params", "store-nothing", times=1)
    host.inject("write_state_slice", "raise")
    transaction, host = make(
        request=BOTH_CHANNELS, host=host, dry_run=False, codec=DerivedCodec()
    )
    result = transaction.run()
    assert not result.ok
    rows = [row for row in result.outcome.named_gaps if row["reason"] == "derived_leaf_not_restored"]
    assert [row["path"] for row in rows] == ["settings.mode"], result.outcome.named_gaps
    assert result.outcome.rollback.complete is False
    assert result.outcome.verdict is not Verdict.FAILED_AND_RESTORED
    assert_counters_consistent(result)


def test_refusals_after_reads_report_the_read_counter():
    """F12: a refusal that follows live reads counts the reads and zero writes.

    The hard dependency is evaluated in the plan stage, i.e. after the token
    check and the live reads; an unknown control, by contrast, is refused in
    preflight. It still costs the two binding reads (P8): a request that *names*
    controls pays for the live ident check before its names are resolved, so an
    unresolvable name cannot skip the check that makes resolvable ones
    addressable. The counter that must stay zero is the write counter.
    """
    transaction, host = make(request={"state": {"settings.mode": "mono"}}, dry_run=False)
    result = transaction.run()
    assert isinstance(result.error, DependencyViolation)
    assert result.outcome.host.reads > 0  # preflight-after-reads is visible
    assert result.outcome.host.write_calls_attempted == 0
    host.reset_log()
    transaction2, host2 = make(request={"controls": {"no_such_control": 1}}, dry_run=False)
    result2 = transaction2.run()
    assert result2.outcome.host.reads == 2  # the binding measurement only
    assert result2.outcome.host.write_calls_attempted == 0
    assert host2.count("param_names") + host2.count("param_idents") == 2
    transaction3, host3 = make(request={"controls": {}}, dry_run=False)
    result3 = transaction3.run()
    assert host3.count("param_names") == 0  # nothing named, nothing to bind
    assert result3.data["preflight"]["binding"]["bound"] == 0


def test_all_seven_tools_register_and_answer_envelopes():
    """AUD-011 sweep: every registered tool answers a schema-2 envelope, and a
    mutator called without dry_run reaches the body with no dry_run argument at
    all (the body's own default decides)."""
    received: dict[str, dict] = {}

    def make_body(name):
        def body(*, config, **kwargs):
            received[name] = dict(kwargs)
            return plugin_tools.ok_envelope(name, {"args": sorted(kwargs)})

        return body

    for name in plugin_tools.TOOL_NAMES:
        plugin_tools.register_dispatch(name, make_body(name))
    try:
        mcp = _RecordingMcp()
        plugin_tools.setup_plugin_tools(mcp, config=None)
        # re-injected after registration (which attaches the P9 production bodies)
        for name in plugin_tools.TOOL_NAMES:
            plugin_tools.register_dispatch(name, make_body(name))
        assert set(mcp.tools) == set(plugin_tools.TOOL_NAMES)
        minimal = {
            "plugin_instances": {},
            "plugin_describe": {"instance": {}},
            "plugin_find": {"instance": {}},
            "plugin_read": {"instance": {}, "controls": []},
            "plugin_write": {"instance": {}, "request": {}},
            "plugin_state": {"instance": {}},
            "plugin_modulation": {"instance": {}, "op": "list"},
        }
        for name, kwargs in minimal.items():
            envelope = mcp.tools[name](**kwargs)
            assert envelope["ok"] is True and envelope["tool"] == name
            assert envelope["schema"] == 2
        # the SPEC's declared default reaches the body (P9: the transport fills
        # every declared argument with None when omitted, so the closure applies
        # the declared default - a supplied False/0 is still kept)
        assert received["plugin_write"]["dry_run"] is True
        assert received["plugin_modulation"]["dry_run"] is True
    finally:
        for name in plugin_tools.TOOL_NAMES:
            plugin_tools.DISPATCH.pop(name, None)
        A.register_production_bodies()
