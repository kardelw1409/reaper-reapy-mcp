"""P38: what ONE state unit costs on the host - the read path, pinned (2026-10-17).

Why this file exists. A live preset apply measured 656 s for ~85 state units, and a
per-primitive probe found the split: the full parameter vector read cost **3.46 s**, of
which **3.03 s** was one ident sweep (`TrackFX_GetParamIdent`, ~1.06 ms per index over
2858 parameters) against 0.46 s for the values themselves. The transaction read the
vector twice per unit (before and after the write), so ~6 s of ~8 s per unit was the
ident table - unchanged, re-read, for the same loaded instance.

The properties that must hold (each is a reason, not a preference):

* the ident table is read **once per transaction**: it is a property of the loaded
  instance, and the target is re-validated before every unit;
* a host-reported parameter count that differs from the cached one **drops** the table -
  pairing cached idents with live values would be a silent mislabelling;
* the VALUE read still surrounds every write (a document write resets ordinary
  parameters - measured), so the saving must never come from reading fewer values;
* one state unit costs exactly **two** chunk reads: the base state it revalidates and the
  read-back it verifies. The third read (a duplicate of the base state) existed until
  this round and bought nothing.

The count behind the last point used to live as an ordinal in
`test_p2_transaction.py::test_a_read_failure_after_a_write_keeps_the_counters_honest`
("read_state_chunk occurrence 4"); that test now aims at the read-back through a hook on
the write, so the *number* is asserted here where its reason is visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
PLUGINS_SRC = Path(__file__).resolve().parents[2] / "src"
if str(PLUGINS_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGINS_SRC))

import fixtures as F  # noqa: E402
import mock_host as MH  # noqa: E402
from mock_provider import MockProvider  # noqa: E402

from reaper_reapy_mcp.plugins.model import PluginInstanceRef  # noqa: E402
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry  # noqa: E402
from reaper_reapy_mcp.plugins.transaction import Transaction  # noqa: E402

#: One path change plus two route removals: three state units (route operations keep
#: their own unit by the declared structural limit), so "per unit" has something to
#: average over. The routes go with the mode change on purpose - the demo provider's
#: hard rule refuses a mode change while routes are active (and evaluating that rule is
#: itself part of what a unit pays for).
MODE_REQUEST = {"state": {"settings.mode": "mono"}, "routes_remove": [{"slot": 1}, {"slot": 2}]}

#: A single state unit: one path with no dependency rule attached to it.
ONE_UNIT = {"state": {"settings.hq": False}}


def build_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, MH.MockFxSlot()])])


def make(*, request: dict, host: MH.MockHostBridge | None = None, fx_index: int = 1):
    host = host if host is not None else build_host()
    provider = MockProvider()
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
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request=request,
        token=token,
        registry=registry,
        dry_run=False,
    )
    return transaction, host


def run(request: dict, host: MH.MockHostBridge | None = None):
    """Run a transaction; answer (result, host, units it wrote, chunk reads it paid)."""
    transaction, host = make(request=request, host=host)
    baseline = host.count("read_state_chunk")
    result = transaction.run()
    assert result.ok, result.error
    units = host.count("write_state_slice")
    assert units >= 1, "the fixture must produce at least one state unit"
    return result, host, units, host.count("read_state_chunk") - baseline


# ---------------------------------------------------------------------------
# The ident table: read once, invalidated by a different table
# ---------------------------------------------------------------------------


def test_the_ident_table_is_read_once_for_the_whole_transaction():
    _result, host, units, _chunks = run(MODE_REQUEST)
    assert units >= 3, "route operations keep their own unit, so the chain has several"
    assert host.count("param_idents") == 1, "the table belongs to the loaded instance"


def test_a_different_parameter_table_can_never_be_paired_with_cached_idents():
    """The primary protection is the TARGET check, measured here (2026-10-17).

    A host that starts reporting a different parameter count is a **different target**
    for this layer (`TargetChanged`), so the transaction refuses and does not write
    against a table it cached for another instance - the cache can never silently pair
    stale idents with live values. The count check inside `_parameter_idents` is the
    second line of the same defence, for a provider whose token does not validate the
    count (it drops the table instead of reusing it).
    """
    host = build_host()
    slot = host.slot(0, 1)

    def grow_table() -> None:
        # the fixture derives BOTH the reported count and the index -> ident table from
        # the ident-keyed store, so this is a table that is no longer the cached one
        slot.params["extra:9001"] = 0.5
        slot.param_names[9001] = "extra_control"

    host.after("write_state_slice", 1, grow_table)
    transaction, host = make(request=MODE_REQUEST, host=host)
    result = transaction.run()
    assert not result.ok, "a target that changed under the transaction must not commit"
    assert not result.committed
    # the refusal names the reason (a rollback that also met the changed target reports
    # it inside its own cause), so the condition is never swallowed into "failed"
    assert "TargetChanged" in json.dumps(result.error.to_dict(), ensure_ascii=True) or any(
        "TargetChanged" in json.dumps(gap, ensure_ascii=True) for gap in result.outcome.named_gaps
    )


# ---------------------------------------------------------------------------
# The chunk reads: two per unit, never three
# ---------------------------------------------------------------------------


def test_one_state_unit_costs_exactly_two_chunk_reads():
    _result, _host, units, chunks = run(ONE_UNIT)
    assert units == 1
    # one read of the live state when the plan is built, then one per unit for the
    # revalidation and one per unit for the read-back that verifies it
    assert chunks == 1 + 2 * units, (
        "the base state and the read-back - a third read of the same chunk buys nothing"
    )


def test_every_unit_costs_two_chunk_reads_in_a_multi_unit_chain():
    _result, _host, units, chunks = run(MODE_REQUEST)
    assert chunks == 1 + 2 * units


# ---------------------------------------------------------------------------
# The VALUE reads: still around every write
# ---------------------------------------------------------------------------


def test_the_value_read_still_surrounds_every_write():
    """The saving must come from the ident table, never from reading fewer values.

    The snapshot captures the full vector once, then every unit re-reads it before its
    write (the expected revision) and after it (a document write resets ordinary
    parameters - measured), so the count grows with the chain.
    """
    _result, host, units, _chunks = run(MODE_REQUEST)
    assert host.count("read_params") >= 1 + 2 * units


def test_the_snapshot_still_captures_the_full_vector():
    """A cheaper read path must not weaken the capture: the restore point stays complete."""
    transaction, host = make(request=MODE_REQUEST)
    captured: list[dict] = []
    original = transaction._read_full_parameter_vector

    def spy() -> dict:
        vector = original()
        captured.append(vector)
        return vector

    transaction._read_full_parameter_vector = spy  # type: ignore[method-assign]
    result = transaction.run()
    assert result.ok, result.error
    assert transaction._snapshot is not None
    assert captured, "the vector must have been read at least once"
    assert len(transaction._snapshot.parameters) == len(captured[0])
    assert len(transaction._snapshot.parameters) == host.slot(0, 1).facts(
        track_index=0, fx_index=1, track_name="Track 1", fx_count=2
    ).param_count


@pytest.mark.parametrize("kind,expected", [("one", 1), ("routes", 3)])
def test_the_ident_table_cost_does_not_grow_with_the_chain(kind: str, expected: int):
    """The property the round was about: the expensive sweep is paid once, not per unit."""
    _result, host, ran, _chunks = run(ONE_UNIT if kind == "one" else MODE_REQUEST)
    assert ran == expected
    assert host.count("param_idents") == 1
