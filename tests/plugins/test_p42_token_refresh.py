"""P42 - a re-created instance must not dead-end the write path (measured live 2026-10-18).

MEASURED, live, through the product surface:

    remove_fx(track_index=14, fx_index=0)            -> success
    add_fx(track_index=14, "VST3i: Vital (Vital Audio)") -> success, fx_index 0
    plugin_sample(op="load", ..., dry_run=False)
        -> TargetChanged: "the write target no longer matches the token's validated facts"
           detail.fields == ["session_handle"], verdict REFUSED_BEFORE_ANY_WRITE, 0 write calls

Everything about the refusal is right except that it was a dead end: the remedy it prints -
"address the instance by location (track_index/fx_index, or the instance object from
plugin_instances)" - is what the caller had just done, and the block would have lasted until the
server process restarted, because the registry is process-scoped (`api._REGISTRY`) and
`TargetTokenRegistry.issue()` reused the one token per validated target *without refreshing its
snapshot*.

It also contradicts the P8 reading that motivated the reuse (`handle_changed: false` on the same
remove-and-re-add). Both measurements stand - the handle moved this time - so the fix is not to
stop comparing it, but to keep the two claims apart:

* a token a **caller** holds is a claim about the past: it still refuses (fail closed);
* the token **this layer** mints for the call in hand is a present-tense claim on that location:
  it is re-anchored to the facts just read, and how often that happened is recorded in its own
  snapshot (`session_handle_changes`) rather than hidden.
"""

from __future__ import annotations

import json

import pytest

import fixtures as F
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import resolve as R
from reaper_reapy_mcp.plugins.errors import TargetChanged
from reaper_reapy_mcp.plugins.model import FxHostFacts

from test_p9_tools import build_host  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def facts(**overrides) -> FxHostFacts:
    payload = dict(F.demo_facts())
    payload.update(overrides)
    return FxHostFacts(**payload)


def write_verbs(host) -> list[str]:
    return [label for label, _detail in host.log if label in ("write_params", "write_state_slice")]


# ---------------------------------------------------------------------------
# The registry: reuse re-anchors, and says so
# ---------------------------------------------------------------------------


def test_reuse_re_anchors_the_snapshot_when_the_handle_moves():
    """Same slot, same build, a handle that moved: the NEXT call is not blocked by it."""
    registry = R.TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts(session_handle="handle-before"))
    # the call in hand happens after the slot was re-populated
    again = registry.issue(facts=facts(session_handle="handle-after"))
    assert again is token, "still one token per validated target: the per-target lock must not move"
    assert len(registry) == 1
    assert token.differences_against(facts(session_handle="handle-after")) == []
    assert registry.revalidate(token.id, facts(session_handle="handle-after")) is token
    assert token.facts_snapshot["session_handle_changes"] == 1


def test_a_closed_reuse_does_not_invent_a_change():
    registry = R.TargetTokenRegistry()
    registry.issue(facts=facts(session_handle="handle-a"))
    registry.issue(facts=facts(session_handle="handle-a"))
    token = registry.issue(facts=facts(session_handle="handle-a"))
    assert token.facts_snapshot["session_handle_changes"] == 0


def test_evidence_appearing_is_recorded_too():
    """`None` -> a handle is not the same reading as `None` -> `None`."""
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts(session_handle=None))
    registry.issue(facts=facts(session_handle="handle-now-readable"))
    assert token.facts_snapshot["session_handle_changes"] == 1
    assert token.facts_snapshot["session_handle"] == "handle-now-readable"


def test_a_caller_held_token_still_refuses_after_the_handle_moved():
    """The past-tense claim is unchanged: the decidable difference is still enforced."""
    registry = R.TargetTokenRegistry()
    token = registry.issue(facts=facts(session_handle="handle-before"))
    with pytest.raises(TargetChanged) as changed:
        registry.revalidate(token.id, facts(session_handle="handle-after"))
    assert changed.value.detail["reason"] == "target_facts_changed"
    assert changed.value.detail["fields"] == ["session_handle"]
    # and the handle itself never reaches a caller
    assert "session_handle" not in json.dumps(token.to_meta())


def test_an_issue_with_the_identity_too_moved_is_a_new_token():
    """A different build is a different target: no reuse, no re-anchoring."""
    registry = R.TargetTokenRegistry()
    first = registry.issue(facts=facts())
    second = registry.issue(facts=facts(class_id=F.FOREIGN_CLASS, fx_name=F.FOREIGN_NAME))
    assert second.id != first.id
    assert len(registry) == 2


# ---------------------------------------------------------------------------
# The live failure, end to end through the write path
# ---------------------------------------------------------------------------


def test_a_re_created_instance_can_be_written_again():
    """The live refusal, reproduced and then closed at the write verb.

    This is the exact sequence the owner asked for ("reset by removing and re-adding the plugin") and
    the exact refusal it produced: the resolution of the call happens AFTER the replacement, so it
    reads the new handle, and the write must land on the instance that is there now.
    """
    from reaper_reapy_mcp.plugins.transaction import Transaction

    host = build_host()
    provider = MockProvider()
    registry = R.TargetTokenRegistry(session_context={"project": "test"})
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
    )
    profile = provider.build_profile(provider.identify(host.fx_facts(0, 1)))

    # an earlier call validated the instance that used to be in this slot
    host.slot(0, 1).session_handle = "handle-before"
    registry.issue(facts=host.fx_facts(0, 1))

    # the plugin is removed and re-added: same slot, same build, a new handle
    host.slot(0, 1).replace_identically(session_handle="handle-after")

    # the next call resolves the instance that is there NOW
    token = registry.issue(facts=host.fx_facts(0, 1))
    result = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request={"controls": {"tone_cutoff": 24.0}},
        token=token,
        registry=registry,
        dry_run=False,
    ).run()
    assert result.ok is True, result.error
    assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
    assert result.outcome.host.write_calls_attempted >= 1
    assert write_verbs(host)
    assert token.facts_snapshot["session_handle_changes"] == 1


def test_a_caller_token_from_before_the_replacement_is_refused_at_the_write_verb():
    """The half that must stay closed: a caller's token is a claim about the past."""
    from reaper_reapy_mcp.plugins.transaction import Transaction

    host = build_host()
    provider = MockProvider()
    registry = R.TargetTokenRegistry(session_context={"project": "test"})
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
    )
    host.slot(0, 1).session_handle = "handle-before"
    stale = registry.issue(facts=host.fx_facts(0, 1))
    profile = provider.build_profile(provider.identify(host.fx_facts(0, 1)))
    host.slot(0, 1).replace_identically(session_handle="handle-after")

    result = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request={"controls": {"tone_cutoff": 24.0}},
        token=stale,
        registry=registry,
        dry_run=False,
    ).run()
    assert result.ok is False
    assert result.error is not None and result.error.code == "TargetChanged"
    assert result.error.detail["fields"] == ["session_handle"]
    assert result.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE
    assert result.outcome.host.write_calls_attempted == 0
    assert write_verbs(host) == []
