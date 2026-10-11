"""P1: model invariants, the revision model and outcome serialization.

Covers P1-2 (revision advance rules) at the object level, the verdict mapping
table (B-45 groundwork) and the [05 §2.9] field-set check.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins.errors import PlanError, StateConflict


# ---------------------------------------------------------------------------
# Verdict -> final_state mapping (11 §3, B-45)
# ---------------------------------------------------------------------------


def test_verdict_to_final_state_table_is_total_and_single_valued():
    assert set(M.VERDICT_FINAL_STATE) == set(M.Verdict)
    assert len(M.VERDICT_FINAL_STATE) == 8
    assert all(isinstance(state, M.FinalState) for state in M.VERDICT_FINAL_STATE.values())
    assert M.VERDICT_FINAL_STATE[M.Verdict.ALL_VERIFIED] == M.FinalState.COMMITTED
    assert M.VERDICT_FINAL_STATE[M.Verdict.NO_WRITES] == M.FinalState.UNCHANGED_VERIFIED
    assert M.VERDICT_FINAL_STATE[M.Verdict.ROLLBACK_FAILED] == M.FinalState.UNKNOWN
    for verdict in M.Verdict:
        assert M.final_state_for(verdict) == M.VERDICT_FINAL_STATE[verdict]


def test_outcome_mapping_consistency_check_accepts_only_the_declared_mapping():
    for verdict, final_state in M.VERDICT_FINAL_STATE.items():
        outcome = M.Outcome(stage=M.Stage.COMMIT, verdict=verdict, final_state=final_state)
        assert outcome.mapping_is_consistent()
    wrong = M.Outcome(
        stage=M.Stage.COMMIT, verdict=M.Verdict.ALL_VERIFIED, final_state=M.FinalState.UNKNOWN
    )
    assert not wrong.mapping_is_consistent()


# ---------------------------------------------------------------------------
# Outcome / counters serialization
# ---------------------------------------------------------------------------


def test_outcome_serializes_to_the_envelope_shape_without_a_token():
    outcome = M.Outcome(
        stage=M.Stage.ROLLBACK,
        verdict=M.Verdict.FAILED_AND_PARTIALLY_RESTORED,
        final_state=M.FinalState.PARTIALLY_RESTORED,
        rollback=M.RollbackReport(attempted=True, verified=False, channels={"state": True, "parameters": False}),
        channels={"state": M.ChannelReport(rows=[{"path": "a.b"}])},
        host=M.HostAccounting(reads=4, write_calls_attempted=2, write_effects_verified=1, write_effects_unknown=1),
        named_gaps=[{"channel": "parameters", "reason": "external_change"}],
    )
    payload = outcome.to_dict()
    text = json.dumps(payload)
    assert set(payload) == {"stage", "verdict", "final_state", "rollback", "channels", "host", "named_gaps"}
    assert payload["verdict"] == "FAILED_AND_PARTIALLY_RESTORED"
    assert payload["final_state"] == "partially_restored"
    assert payload["rollback"]["channels"] == {"state": True, "parameters": False}
    assert payload["host"]["write_effects_unknown"] == 1
    assert "tok_" not in text


def test_host_accounting_zero_writes_rule():
    assert M.HostAccounting().zero_writes
    assert not M.HostAccounting(write_calls_attempted=1).zero_writes


# ---------------------------------------------------------------------------
# State revision
# ---------------------------------------------------------------------------


def test_revision_is_deterministic_and_content_addressed():
    base = M.StateRevision.compute(state_channel="block-a", parameters={"3:1003": 0.5})
    again = M.StateRevision.compute(state_channel="block-a", parameters={"3:1003": 0.5})
    other_state = M.StateRevision.compute(state_channel="block-b", parameters={"3:1003": 0.5})
    other_params = M.StateRevision.compute(state_channel="block-a", parameters={"3:1003": 0.6})
    assert base.digest == again.digest
    assert base.digest != other_state.digest
    assert base.digest != other_params.digest


def test_missing_channel_is_not_the_empty_channel():
    missing_state = M.StateRevision.compute(state_channel=None, parameters={"1:1": 0.5})
    empty_state = M.StateRevision.compute(state_channel="", parameters={"1:1": 0.5})
    missing_params = M.StateRevision.compute(state_channel="block", parameters=None)
    empty_params = M.StateRevision.compute(state_channel="block", parameters={})
    assert missing_state.digest != empty_state.digest
    assert missing_params.digest != empty_params.digest


def test_revision_needs_at_least_one_channel():
    with pytest.raises(ValueError):
        M.StateRevision.compute(state_channel=None, parameters=None)


# ---------------------------------------------------------------------------
# RevisionTracker (P1-2)
# ---------------------------------------------------------------------------


def test_tracker_refuses_to_check_without_an_expected_revision():
    tracker = M.RevisionTracker()
    with pytest.raises(PlanError):
        tracker.check(M.StateRevision.compute(state_channel="a", parameters=None))


def test_tracker_reports_external_change_and_never_self_conflicts():
    tracker = M.RevisionTracker()
    r0 = M.StateRevision.compute(state_channel="s0", parameters={"1:1": 0.5})
    tracker.seed(r0)
    tracker.check(r0)
    external = M.StateRevision.compute(state_channel="s-ext", parameters={"1:1": 0.9})
    with pytest.raises(StateConflict) as excinfo:
        tracker.check(external)
    assert excinfo.value.detail["expected"] == r0.digest
    assert excinfo.value.detail["observed"] == external.digest

    r1 = M.StateRevision.compute(state_channel="s1", parameters={"1:1": 0.6})
    tracker.advance_from_verified_readback(r1)
    tracker.check(r1)  # the transaction's own verified write is not a conflict
    assert tracker.is_own_verified_write(r1) is True
    assert tracker.is_own_verified_write(r0) is False
    with pytest.raises(StateConflict):
        tracker.check(external)


def test_is_own_verified_write_is_tri_state():
    tracker = M.RevisionTracker()
    assert tracker.is_own_verified_write(
        M.StateRevision.compute(state_channel="x", parameters=None)
    ) is None


# ---------------------------------------------------------------------------
# Slice / snapshot invariants ([05 §2.9])
# ---------------------------------------------------------------------------


def test_slice_is_only_valid_for_its_own_chunk():
    chunk = "a\nb\nc"
    slice_ = M.TargetedStateSlice(
        chunk_digest=M.text_digest(chunk),
        line_start=1,
        line_end=1,
        header_line="<VST ...>",
        class_hex="AA",
        ordinal=0,
        block_text="b",
    )
    assert slice_.is_valid_for(chunk)
    assert not slice_.is_valid_for(chunk + "d")
    assert slice_.block_digest == M.text_digest("b")


def test_snapshot_completeness_helpers():
    snapshot = M.StateSnapshot(
        document={},
        routes=[],
        parameters={"1:1": 0.5},
        manifest={"captured": ["document", "routes", "parameters"], "not_captured": []},
    )
    assert snapshot.is_complete_for(["document", "routes", "parameters"])
    partial = M.StateSnapshot(
        document={}, routes=[], parameters={}, manifest={"captured": ["document"], "not_captured": ["parameters"]}
    )
    assert partial.missing_channels(["document", "routes", "parameters"]) == ["routes", "parameters"]
    assert not partial.is_complete_for(["document", "parameters"])


def test_section_2_9_objects_carry_the_normative_fields():
    expected = {
        M.FxHostFacts: {"track_index", "fx_index", "fx_name", "class_id", "module", "param_count", "version"},
        M.TargetedStateSlice: {"chunk_digest", "line_start", "line_end", "header_line", "class_hex", "ordinal", "block_text"},
        M.StateRevision: {"digest", "scope", "read_utc"},
        M.StateSnapshot: {"document", "routes", "parameters", "base_revision", "manifest"},
        M.StateWriteUnit: {"order_index", "kind", "payload", "expected_revision", "target_token_id", "preconditions"},
        M.StateWritePlan: {"units", "rollback_plan", "verification_plan"},
        M.WriteAttempt: {"unit_index", "attempted_utc", "returned", "slice_digest_sent", "readback_slice_digest", "effects_verified"},
        M.VerificationReport: {"ok", "rows", "mismatched", "comparators", "tolerances", "space"},
    }
    for klass, fields in expected.items():
        present = {field.name for field in dataclasses.fields(klass)}
        assert fields <= present, f"{klass.__name__} lacks {fields - present}"
