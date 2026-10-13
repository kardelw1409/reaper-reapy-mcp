"""P36: the ident path's read budget - use-time resolution + ONE merged read.

Measured live before this round (``probe_p36_write_cost``, phase "before"): one
1-control write through the provider path cost **24,549 host calls / 21.9 s**,
of which ``TrackFX_GetParamIdent`` accounted for **15,066 calls** (~1 ms each,
14.97 s) - five whole-table sweeps of 2858 parameters plus the 775-control
binding - while the providerless path cost 96 calls / 4.0 s. Two properties
changed in the transaction:

1. ``_indices_for`` resolves each ident through the direct
   ``param_index_from_idents`` bridge verb (one host call per ident, the same
   rule the write hop applies inside its own hop) instead of building a
   whole-table map;
2. the post-write read serves BOTH the read-back comparison and the revision
   basis - exactly ONE read happens after the write, so the row a caller
   verifies is the state the tracker advances from (a second read could observe
   a state the comparison never saw).

Everything here is offline: the real ``Transaction`` over the mock host, where
the call log is the evidence.
"""

from __future__ import annotations

import pytest

from reaper_reapy_mcp.plugins import binding as B
from reaper_reapy_mcp.plugins.errors import ReadbackMismatch, SnapshotUnavailable
from reaper_reapy_mcp.plugins.model import Verdict

from test_p2_transaction import build_host, make  # the P2 mock-host harness

BATCH = {
    "controls": {
        "tone_cutoff": 24.0,
        "tone_level": 0.5,
        "mix_percent": 60.0,
        "tone_shape": "soft",
    }
}


def _labels(host) -> list[str]:
    return [label for label, _detail in host.log]


def _reads_after(host, write_label: str) -> list[str]:
    labels = _labels(host)
    index = max(position for position, label in enumerate(labels) if label == write_label)
    return [label for label in labels[index + 1:] if label == "read_params"]


def _assert_counters(result) -> None:
    accounting = result.outcome.host
    assert accounting.write_calls_attempted == (
        accounting.write_effects_verified + accounting.write_effects_unknown
    ), accounting.as_dict()
    assert result.outcome.mapping_is_consistent()


# ---------------------------------------------------------------------------
# 1. one read after the write (both addressing paths)
# ---------------------------------------------------------------------------


def test_one_read_after_the_write_serves_verify_and_revision_ident_path():
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0}}, dry_run=False)
    result = transaction.run()
    assert result.ok and result.committed, result.error
    assert result.outcome.verdict is Verdict.ALL_VERIFIED
    assert _reads_after(host, "write_params") == ["read_params"], (
        "the read-back comparison and the revision basis must be the SAME read"
    )
    rows = result.outcome.channels["parameters"].rows
    assert rows and all(row["agrees"] for row in rows)
    _assert_counters(result)


def test_one_read_after_the_write_for_a_four_control_batch():
    transaction, host = make(request=BATCH, dry_run=False)
    result = transaction.run()
    assert result.ok and result.committed, result.error
    assert host.count("write_params") == 1, "one batched write"
    assert _reads_after(host, "write_params") == ["read_params"]
    assert len(result.outcome.channels["parameters"].rows) == 4
    _assert_counters(result)


# ---------------------------------------------------------------------------
# 2. use-time ident resolution; no whole-table sweep on the read path
# ---------------------------------------------------------------------------


def test_the_read_path_resolves_idents_directly_and_never_sweeps():
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0}}, dry_run=True)
    result = transaction.run()
    assert result.ok, result.error
    assert host.count("param_index_from_idents") >= 1, (
        "the planned read must resolve the ident at use time"
    )
    param_count = host.fx_facts(0, 1).param_count
    indexed = len(B.indexed_controls(transaction.profile))
    ident_reads = [detail.get("n") for label, detail in host.log if label == "param_idents"]
    assert ident_reads, "the binding still reads the declared indices"
    assert ident_reads.count(param_count) == 1, (
        "exactly ONE whole-table ident read is allowed - the transaction's declared "
        f"full-vector capture; the lookup path must not sweep: {ident_reads}"
    )
    assert [n for n in ident_reads if n == indexed] == [indexed], (
        f"the binding reads the {indexed} declared indices; got {ident_reads}"
    )


def test_a_missing_ident_refuses_with_the_historical_message():
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0}}, dry_run=True)
    with pytest.raises(SnapshotUnavailable) as excinfo:
        transaction._indices_for(["ident-not-in-table"])
    assert excinfo.value.message == "the parameter ident is not present in the live parameter table"
    assert excinfo.value.detail["missing"] == ["ident-not-in-table"]
    assert host.count("param_index_from_idents") == 1


def test_a_short_ident_resolution_is_not_evidence():
    """A bridge answering fewer indices than asked refuses, like every sibling read."""
    host = build_host()
    host.inject("param_index_from_idents", "store-nothing", times=1)
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0}}, host=host, dry_run=True)
    result = transaction.run()
    assert not result.ok
    assert isinstance(result.error, SnapshotUnavailable), result.error
    assert result.error.detail == {"expected": 1, "read": 0}
    assert host.count("write_params") == 0


# ---------------------------------------------------------------------------
# 3. a mismatched read-back still refuses and restores
# ---------------------------------------------------------------------------


def test_a_mismatched_readback_still_refuses_and_never_overwrites_foreign_state():
    """The read-back mismatch is refused; the diverged value is NOT overwritten.

    The mock's `store-wrong` write lands a value the transaction never sent, so
    the live state matches neither the captured baseline nor the attempted
    digest - the rollback classifies it as foreign and leaves it (Decision 3),
    reporting the gap instead of guessing a restore. This behaviour is unchanged
    by the merged read; the test pins it so the merge cannot hide it.
    """
    host = build_host()
    host.inject("write_params", "store-wrong", times=1)
    transaction, host = make(request={"controls": {"tone_cutoff": 24.0}}, host=host, dry_run=False)
    result = transaction.run()
    assert not result.ok
    assert isinstance(result.error, ReadbackMismatch), result.error
    rows = result.outcome.channels["parameters"].rows
    assert rows and rows[0]["agrees"] is False, rows
    assert result.outcome.rollback.attempted is True
    assert result.outcome.verdict is Verdict.FAILED_AND_PARTIALLY_RESTORED
    assert any(
        gap["reason"] == "not_restored (external_change)" for gap in result.outcome.named_gaps
    ), result.outcome.named_gaps
    assert result.outcome.rollback.channels.get("parameters") is False
    _assert_counters(result)


# ---------------------------------------------------------------------------
# 4. the providerless (index-addressed) path: the same one-read property
# ---------------------------------------------------------------------------


def test_one_read_after_the_write_for_the_providerless_path(tmp_path, monkeypatch):
    """The index path's post-write read is also the single merged read.

    The ORDER is captured by wrapping the fake bridge's verbs (its call counters
    cannot express "after the write").
    """
    from mcp.server.fastmcp import FastMCP

    from reaper_reapy_mcp.plugins import api as A
    from reaper_reapy_mcp.plugins import tools as T
    from test_p28_providerless import call, prepared_env

    bridge, _directory = prepared_env(tmp_path, monkeypatch)
    order: list[str] = []
    original_read = bridge.read_params
    original_write = bridge.write_params_by_index

    def read_params(ref, indices):
        order.append("read_params")
        return original_read(ref, indices)

    def write_params_by_index(ref, pairs):
        order.append("write_params_by_index")
        return original_write(ref, pairs)

    bridge.read_params = read_params
    bridge.write_params_by_index = write_params_by_index

    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("p36-merge")
    T.setup_plugin_tools(mcp, config=config)
    envelope = call(
        mcp,
        "plugin_write",
        {
            "instance": {"track_index": 0, "fx_index": 0},
            "request": {"controls": {"filter_1_cutoff": 100.0, "octave": 2}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    write_at = order.index("write_params_by_index")
    assert order[write_at + 1:] == ["read_params"], (
        f"exactly one read may follow the write; got {order[write_at + 1:]}"
    )
    assert envelope["outcome"]["verdict"] == "ALL_VERIFIED"
