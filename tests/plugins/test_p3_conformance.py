"""P3: host-bridge read conformance, run against the mock and the real bridge.

The same read assertions run against `MockHostBridge` (always, offline) and
`ReaperHostBridge` (when REAPER is reachable; skipped with an explicit reason
otherwise). A live run is READ-ONLY: enumeration, identity facts, batched
parameter reads and slice location - no write verb exists in the module.
"""

from __future__ import annotations

import pytest

import fixtures as F
import mock_host as MH

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins.errors import NotConnected, Unreadable
from reaper_reapy_mcp.plugins.host import reaper as R


def _read_suite(bridge, *, counts_exposed: bool) -> None:
    tracks = bridge.list_tracks()
    assert tracks, "the host must report at least one track"
    assert all(isinstance(track.n_fx, int) for track in tracks)

    track = tracks[0]
    if track.n_fx == 0:
        pytest.skip("the first track has no FX; the live read suite has nothing to read")

    facts = bridge.fx_facts(track.track_index, 0)
    assert isinstance(facts.fx_name, str)
    assert isinstance(facts.param_count, int) and facts.param_count >= 0

    chunk = bridge.read_state_chunk(
        M.PluginInstanceRef(
            host="reaper",
            location={"track_index": track.track_index, "fx_index": 0},
            provider="demo",
            identity={},
        )
    )
    assert isinstance(chunk, str) and chunk
    blocks = R.parse_blocks(chunk)
    for block in blocks:
        if not block["class_hex"] and not block["module"]:
            continue
        slice_ = R.locate_block_span(chunk, class_hex=block["class_hex"], module=block["module"])
        assert slice_.is_valid_for(chunk)
        assert slice_.block_text == block["block_text"]
        before, after = F.split_outside(chunk, slice_.line_start, slice_.line_end)
        assert before + "\n" + slice_.block_text + "\n" + after == chunk

    if facts.param_count:
        indices = list(range(min(8, facts.param_count)))
        ref = M.PluginInstanceRef(
            host="reaper",
            location={"track_index": track.track_index, "fx_index": 0},
            provider="demo",
            identity={},
        )
        idents = bridge.param_idents(ref, indices)
        values = bridge.read_params(ref, indices)
        assert len(idents) == len(indices) == len(values)
        assert all(value is None or isinstance(value, str) for value in idents)
        assert all(0.0 <= float(value) <= 1.0 for value in values)
        if counts_exposed:
            # one host hop per batch, not per control
            assert bridge.count("read_params") >= 1
            assert bridge.count("param_idents") >= 1


def test_read_conformance_against_the_mock_bridge():
    _read_suite(MH.MockHostBridge(), counts_exposed=True)


def test_read_conformance_against_the_real_bridge_when_reachable():
    bridge = R.ReaperHostBridge(timeout=10.0)
    try:
        bridge.list_tracks()
    except NotConnected as exc:
        pytest.skip(f"REAPER is not reachable, so the live read suite did not run: {exc.message}")
    except Unreadable as exc:
        pytest.skip(f"the live host answered with an unreadable contract: {exc.message}")
    _read_suite(bridge, counts_exposed=False)


# ---------------------------------------------------------------------------
# Chunk header parsing: the module token (P27)
# ---------------------------------------------------------------------------


def _chunk_with_header(header: str) -> str:
    return "\n".join(["<TRACK", "  NAME t", header, "  >", ">"])


def test_a_quoted_module_token_is_parsed_whole():
    """REAPER quotes a module token that contains a space.

    Measured live 2026-10-12: VST3 Surge XT's header carries
    ``"Surge XT.vst3"`` and a bare ``(\\S+)`` capture truncated it to ``"Surge``;
    Vital's space-free ``Vital.vst3`` parsed correctly and hid the defect. The
    module string is load-bearing identity evidence (it is what distinguishes two
    builds that share a class id), so a truncated token is a wrong answer.
    """
    header = (
        '<VST "VST3i: Surge XT (Surge Synth Team) (2->6ch)" "Surge XT.vst3" 0 "" '
        '661331015{ABCDEF019182FAEB566D624153675854} ""'
    )
    chunk = _chunk_with_header(header)
    for parser in (R.parse_blocks, MH.parse_blocks):
        blocks = parser(chunk)
        assert len(blocks) == 1, parser.__module__
        block = blocks[0]
        assert block["name"] == "VST3i: Surge XT (Surge Synth Team) (2->6ch)"
        assert block["module"] == "Surge XT.vst3"
        assert block["class_hex"] == "ABCDEF019182FAEB566D624153675854"


def test_a_bare_module_token_is_unchanged():
    """The space-free form (what Vital writes) keeps parsing exactly as before."""
    header = (
        '<VST "VST3i: Vital (Vital Audio)" Vital.vst3 0 "" '
        '1854596000{56535456697461766974616C00000000} ""'
    )
    chunk = _chunk_with_header(header)
    for parser in (R.parse_blocks, MH.parse_blocks):
        block = parser(chunk)[0]
        assert block["name"] == "VST3i: Vital (Vital Audio)"
        assert block["module"] == "Vital.vst3"
        assert block["class_hex"] == "56535456697461766974616C00000000"


def test_a_quoted_empty_module_is_no_evidence_not_a_literal():
    header = '<VST "VST3i: Some Plugin" "" 0 "" 1234{ABCDEF019182FAEB566D624153675854} ""'
    chunk = _chunk_with_header(header)
    for parser in (R.parse_blocks, MH.parse_blocks):
        block = parser(chunk)[0]
        assert block["module"] is None


def test_wedge_chokepoint_never_hangs():
    """A host call that never returns is classified, not waited on."""
    import threading

    from reaper_reapy_mcp.plugins.errors import Wedged

    bridge = R.ReaperHostBridge(timeout=0.05)
    never = threading.Event()
    with pytest.raises(Wedged) as excinfo:
        bridge.call(lambda: never.wait(30), timeout=0.05, label="never-returns")
    assert excinfo.value.detail["timeout_s"] == 0.05
    assert excinfo.value.detail["label"] == "never-returns"


def test_transport_failure_is_classified_as_not_connected():
    bridge = R.ReaperHostBridge(timeout=5.0)

    def boom() -> None:
        raise ConnectionRefusedError("no listener")

    with pytest.raises(NotConnected):
        bridge.call(boom, timeout=5.0, label="connect-probe")
