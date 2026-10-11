"""P1-1 / P1-5: write-target tokens, revalidation, and the no-leak rule."""

from __future__ import annotations

import json

import pytest

import fixtures as F

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import resolve as R
from reaper_reapy_mcp.plugins.errors import TargetChanged
from reaper_reapy_mcp.plugins.model import FxHostFacts


def facts(**overrides) -> FxHostFacts:
    payload = dict(F.demo_facts())
    payload.update(overrides)
    return FxHostFacts(**payload)


def test_token_issued_from_fresh_facts():
    registry = R.TargetTokenRegistry(session_context={"project": "demo-project"})
    token = registry.issue(facts=facts())
    assert token.id.startswith("tok_") and len(token.id) == 36
    assert token.location == {"track_index": 0, "fx_index": 0}
    assert token.identity["class_id"] == F.DEMO_CLASS
    assert token.identity["param_count"] == len(F.DEMO_PARAMS)
    assert registry.session_context == {"project": "demo-project"}


def test_valid_token_revalidates_against_unchanged_facts():
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts())
    assert registry.revalidate(token.id, facts()) is token
    # a None session handle or an absent version must not be a mismatch
    assert registry.revalidate(token.id, facts()) is token


def test_stale_token_is_refused_with_the_fields_that_moved():
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts())
    with pytest.raises(TargetChanged) as excinfo:
        registry.revalidate(token.id, facts(fx_name="VST3i: Other Synth"))
    detail = excinfo.value.detail
    assert detail["fields"] == ["fx_name"]
    assert detail["expected"]["identity"]["fx_name"] == F.DEMO_NAME
    assert detail["observed"]["identity"]["fx_name"] == "VST3i: Other Synth"
    assert len(registry) == 1  # nothing was silently re-targeted


def test_location_change_is_detected():
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts())
    with pytest.raises(TargetChanged) as excinfo:
        registry.revalidate(token.id, facts(fx_index=1))
    assert excinfo.value.detail["fields"] == ["location"]


def test_identity_build_change_is_detected():
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts())
    with pytest.raises(TargetChanged) as excinfo:
        registry.revalidate(token.id, facts(class_id="00000000000000000000000000000000", param_count=12))
    fields = excinfo.value.detail["fields"]
    assert "class_id" in fields and "param_count" in fields


def test_a_changed_session_handle_is_a_difference_but_l3_stays_open():
    """The handle is compared; the live measurement says it does not close L-3.

    Measured live (P8, 2026-10-10): removing an FX and re-adding the same build at
    the same slot reproduced the **same** handle, while a second instance at
    another slot had a different one - so the handle identifies the slot, not the
    instance, and an identical-build replacement remains indistinguishable. What
    is tested here is only that a handle that *does* change is refused (fail
    closed) and that the handle never reaches a caller.
    """
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts(session_handle="handle-slot-0"))
    # the same slot, the same build, a handle that moved: refused
    with pytest.raises(TargetChanged) as excinfo:
        registry.revalidate(token.id, facts(session_handle="handle-elsewhere"))
    assert excinfo.value.detail["fields"] == ["session_handle"]
    assert len(registry) == 1, "no silent re-target"

    # the host no longer reporting a handle is a difference too: no evidence is
    # not the same as evidence of sameness
    with pytest.raises(TargetChanged) as excinfo2:
        registry.revalidate(token.id, facts(session_handle=None))
    assert excinfo2.value.detail["fields"] == ["session_handle_unreadable"]

    # a token issued when the host reported no handle cannot compare one, and
    # that absence is not turned into a pass or a failure (a fresh registry, since
    # `issue` deliberately reuses the one token per validated target)
    other = R.TargetTokenRegistry()
    unhandled = other.issue(facts=facts(session_handle=None))
    assert other.revalidate(unhandled.id, facts(session_handle="handle-now-readable")) is unhandled
    assert "session_handle" not in unhandled.to_meta()
    assert "session_handle" not in json.dumps(unhandled.to_meta())


def test_unknown_or_expired_token_is_refused():
    registry = R.TargetTokenRegistry()
    with pytest.raises(TargetChanged):
        registry.revalidate("tok_deadbeef", facts())


def test_token_repr_and_meta_never_expose_the_whole_id_through_repr():
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts())
    assert token.id not in repr(token)
    assert token.id not in repr(registry)
    assert token.redacted_id.endswith("…")
    # meta is the ONE sanctioned carrier of the opaque id (06 §2)
    assert token.to_meta()["target_token"] == token.id
    assert "facts_snapshot" not in token.to_meta()


def test_serialized_outcome_never_contains_the_token():
    """P1-5: any serialized outcome is scanned for the token - absent."""
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts())
    outcome = M.Outcome(
        stage=M.Stage.COMMIT,
        verdict=M.Verdict.ALL_VERIFIED,
        final_state=M.FinalState.COMMITTED,
        host=M.HostAccounting(reads=1, write_calls_attempted=1, write_effects_verified=1),
    )
    text = json.dumps(outcome.to_dict())
    assert token.id not in text
    assert token.redacted_id not in text


def test_identity_dict_omits_an_absent_version():
    identity = R.facts_identity(FxHostFacts(track_index=0, fx_index=0, fx_name="x", version=None))
    assert "version" not in identity
    identity2 = R.facts_identity(FxHostFacts(track_index=0, fx_index=0, fx_name="x", version="1.0.0"))
    assert identity2["version"] == "1.0.0"
