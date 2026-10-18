"""P11 — Decisions 2 and 5: the derived leaf and the canonical gap.

Owner position (2026-10-10): the plugin-owned `settings.sample.samples` blob keeps
its limitation - it is never a write target and is never restored by writing the
old bytes back - but the **representation** of that limitation becomes uniform:

- `derived_excluded` (a codec change-set/restore field) is the *mechanism*: which
  captured leaves the restore set leaves out;
- `derived_leaf_not_restored` (an outcome `named_gaps` entry) is the
  *consequence*: one row per excluded leaf, present on every path where a restore
  ran - failed, partially restored, or successfully restored everything else;
- `rollback.verified` (what was written back landed) is kept apart from
  `rollback.complete` (that set covered the whole capture), so "the remaining
  channels are back" is never reported as "everything is back".

The last checks drive the **registered MCP tool**, because a gap that cannot
survive the Core -> envelope boundary is not reported at all.
"""

from __future__ import annotations

import json

import pytest

from mock_provider import DemoCodec, MockProvider

from reaper_reapy_mcp.plugins.errors import InvalidValue
from reaper_reapy_mcp.plugins.model import PluginInstanceRef, Verdict
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry
from reaper_reapy_mcp.plugins.transaction import Transaction

from test_p9_tools import build_host, call, demo  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: A captured leaf that neither the demo dependency rule nor the codec special-cases.
DERIVED = "settings.hq"
INSTANCE = {"track_index": 0, "fx_index": 1}
GAP_DETAIL = "the provider declares this leaf as plugin-derived; a document write cannot set it"


class DerivedLeafCodec(DemoCodec):
    """A codec that declares one captured leaf plugin-derived (the Vital shape)."""

    def restore_changes(self, doc, snap):
        changes = super().restore_changes(doc, snap)
        changes["paths"].pop(DERIVED, None)
        changes.setdefault("derived_excluded", []).append(DERIVED)
        return changes


class DerivedLeafProvider(MockProvider):
    def codec(self):
        return DerivedLeafCodec()


def gap_rows(outcome: dict) -> list[dict]:
    return [row for row in outcome["named_gaps"] if row["reason"] == "derived_leaf_not_restored"]


# ---------------------------------------------------------------------------
# The internal outcome
# ---------------------------------------------------------------------------


def test_a_successful_rollback_of_everything_else_still_names_the_gap():
    """`verified: true` + `complete: false` + the gap: the three go together."""
    host = build_host()
    host.inject("write_params", "store-nothing", times=1)  # the batch fails to land

    from test_p2_transaction import make

    transaction, host = make(
        request={"state": {DERIVED: "mono"}, "controls": {"tone_cutoff": 24.0}},
        host=host,
        dry_run=False,
        codec=DerivedLeafCodec(),
    )
    result = transaction.run()
    assert not result.ok
    payload = result.outcome.to_dict()
    assert payload["rollback"]["verified"] is True
    assert payload["rollback"]["complete"] is False
    assert payload["rollback"]["excluded"] == [DERIVED]
    assert payload["rollback"]["channels"] == {"state": True, "parameters": True}
    assert gap_rows(payload) == [
        {"channel": "state", "reason": "derived_leaf_not_restored", "path": DERIVED, "detail": GAP_DETAIL}
    ]
    # the honest verdict for "verified but not complete"
    assert result.outcome.verdict is Verdict.FAILED_AND_PARTIALLY_RESTORED
    assert payload["final_state"] == "partially_restored"


def test_the_two_representations_agree_and_each_has_one_meaning():
    """`derived_excluded` is the mechanism; the gap is the consequence."""
    host = build_host()
    host.inject("write_params", "store-nothing", times=1)
    from test_p2_transaction import make

    transaction, host = make(
        request={"state": {DERIVED: "mono"}, "controls": {"tone_cutoff": 24.0}},
        host=host,
        dry_run=False,
        codec=DerivedLeafCodec(),
    )
    result = transaction.run()
    payload = result.outcome.to_dict()
    excluded = payload["rollback"]["excluded"]
    gaps = gap_rows(payload)
    assert [row["path"] for row in gaps] == excluded, (
        "the same fact must not appear in two shapes with different content"
    )
    assert all(row["channel"] == "state" for row in gaps)


def test_the_derived_leaf_cannot_be_a_write_target_at_all():
    """It is refused before any write - and the refusal is identical in a dry run."""
    host = build_host()
    provider = mock_provider_for_codec()
    facts = host.fx_facts(0, 1)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    ref = PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 1}, provider="demo", identity={}
    )
    for dry_run in (True, False):
        result = Transaction(
            provider=provider,
            codec=provider.codec(),
            bridge=host,
            profile=provider.build_profile(provider.identify(facts)),
            ref=ref,
            request={"state": {DERIVED: "mono"}},
            token=registry.issue(facts=facts),
            registry=registry,
            dry_run=dry_run,
        ).run()
        assert result.ok is False and isinstance(result.error, InvalidValue), dry_run
        assert result.error.detail["reason"] == "derived_leaf_not_writable"
        assert result.outcome.verdict is Verdict.REFUSED_BEFORE_ANY_WRITE
        assert result.outcome.host.write_calls_attempted == 0
    assert [label for label, _d in host.log if label.startswith("write")] == []


def mock_provider_for_codec() -> MockProvider:
    """A provider whose codec refuses the derived leaf exactly as Vital's does."""

    class Codec(DerivedLeafCodec):
        derived_leaves = (DERIVED,)

        def restore_changes(self, doc, snap):
            return super().restore_changes(doc, snap)

        def plan_state_writes(self, doc, changes, *, other_track_chars: int = 0):
            from reaper_reapy_mcp.plugins.errors import InvalidValue

            for path in (changes.get("paths") or {}):
                if str(path) in self.derived_leaves:
                    raise InvalidValue(
                        f"state path {path!r} is plugin-derived",
                        detail={
                            "path": str(path),
                            "reason": "derived_leaf_not_writable",
                            "derived_leaves": list(self.derived_leaves),
                        },
                    )
            return super().plan_state_writes(doc, changes)

    class Provider(MockProvider):
        def codec(self):
            return Codec()

    return Provider()


# ---------------------------------------------------------------------------
# The public MCP envelope (the boundary the gap must survive)
# ---------------------------------------------------------------------------


def test_the_gap_survives_the_core_to_mcp_serialization(demo):
    """Driven end to end through the registered tool."""
    demo["factory"]["make"] = lambda: DerivedLeafProvider()
    demo["host"].inject("write_params", "store-nothing", times=1)
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": INSTANCE,
            "request": {"state": {DERIVED: "mono"}, "controls": {"tone_cutoff": 24}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] in ("ReadbackMismatch", "WriteNotApplied"), envelope["error"]
    outcome = envelope["outcome"]
    assert outcome["rollback"]["verified"] is True
    assert outcome["rollback"]["complete"] is False
    assert outcome["rollback"]["excluded"] == [DERIVED]
    assert gap_rows(outcome) == [
        {"channel": "state", "reason": "derived_leaf_not_restored", "path": DERIVED, "detail": GAP_DETAIL}
    ]
    assert outcome["verdict"] == "FAILED_AND_PARTIALLY_RESTORED"
    assert outcome["final_state"] == "partially_restored"
    assert json.dumps(envelope), "the envelope must serialize with the gap in it"


def test_a_successful_write_reports_completeness_without_a_gap(demo):
    """The positive control at the public boundary."""
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": INSTANCE,
            "request": {"state": {DERIVED: "mono"}, "controls": {"tone_cutoff": 24}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    assert envelope["outcome"]["verdict"] == "ALL_VERIFIED"
    assert envelope["outcome"]["named_gaps"] == []


def test_no_restore_attempted_never_reports_complete(demo):
    """`complete` is a statement about a restore that ran (audit finding).

    A committed write that rolled nothing back reports `attempted: false`,
    `verified: false` and `complete: false`: a consumer must never read a default
    as "everything came back".
    """
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": INSTANCE, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": False},
    )
    assert envelope["ok"] is True, envelope
    rollback = envelope["outcome"]["rollback"]
    assert rollback["attempted"] is False
    assert rollback["verified"] is False
    assert rollback["complete"] is False
    assert rollback["excluded"] == []


def test_the_tool_refuses_a_direct_write_to_a_declared_derived_leaf(demo):
    demo["factory"]["make"] = lambda: mock_provider_for_codec()
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": INSTANCE, "request": {"state": {DERIVED: "mono"}}, "dry_run": False},
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "InvalidValue"
    assert envelope["error"]["detail"]["reason"] == "derived_leaf_not_writable"
    assert envelope["outcome"]["verdict"] == "REFUSED_BEFORE_ANY_WRITE"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert demo["host"].count("write_state_slice") == 0
