"""P42 - the read ceiling belongs to the TRACK, not to one instance (measured live 2026-10-18).

MEASURED, live, through the product surface:

    plugin_sample(op="load", file_path="One Shot - Ahhh Sustained.wav",
                  instance={"track_index": 14, "fx_index": 1}, dry_run=False)
        -> RollbackFailed
           cause: Unreadable "the host faulted while reading read_state_chunk: the plugin's state
                  is larger than this channel can read"
           outcome: verdict ROLLBACK_FAILED, write_effects_unknown: 1

The track already carried a Vital instance whose own state was ~3.06 M chunk characters (75 % of
the 4.1 M read limit), and the second instance's balloon added ~3.8 M more INSIDE THE SAME TRACK
CHUNK. `GetTrackStateChunk` then faulted on every read of that track: the write could not be
verified, the rollback could not read either, and the state had to be recovered from OUTSIDE the
layer (`remove_fx`, a host operation that reads no chunk) - the exact outcome the ceiling exists to
prevent.

The cause was a unit mix-up: `_state_ceiling_chars()` budgeted ONE document (~3.06 M document
characters) while the host's limit is on the whole track's chunk text (~4.1 M). Each instance was
inside its own budget and the sum was not. So the planner now knows what else the track holds:

* `TargetedStateSlice.chunk_chars` carries the whole chunk's length from the bridge (0 = unknown);
* `Transaction._other_track_chars()` turns it into "everything that is not this block";
* `StateCodec.plan_state_writes(..., other_track_chars=...)` receives it and subtracts it from the
  measured ceiling before dividing by the base64 expansion;
* the refusal names the deduction, so a caller is told to free room on the TRACK.

Mock host + fixtures only: no live REAPER.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import transaction as TR
from reaper_reapy_mcp.plugins.providers.vital import codec as VC

import test_p7_vital as T7
import test_p39_balloon as P39
import test_p39c_audio_slots as P39C

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


def host_with(document: dict) -> MH.MockHostBridge:
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(document))])]
    )
    host.slot(0, 0).version = LIVE_VERSION
    return host


def ref() -> M.PluginInstanceRef:
    return M.PluginInstanceRef(
        host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
    )


def decode_live(host: MH.MockHostBridge) -> dict:
    codec = T7.provider_on_fixtures().codec()
    return codec.decode(
        host.locate_state_slice(ref(), host.fx_facts(0, 0), host.read_state_chunk(ref()))
    )


# ---------------------------------------------------------------------------
# 1. The arithmetic: what the rest of the track holds comes off first
# ---------------------------------------------------------------------------


def test_the_ceiling_shrinks_by_what_the_rest_of_the_track_holds():
    """A deduction in CHUNK characters becomes a deduction in DOCUMENT characters.

    The two spaces are not the same size (the chunk carries the document as base64, x4/3), and
    mixing them is exactly how the live incident happened: the plan was measured in documents while
    the host's limit is on the track's chunk text.
    """
    base = VC._state_ceiling_chars()
    assert base == int(
        (VC.STATE_CHUNK_CEILING_CHARS - VC.STATE_CEILING_MARGIN_CHARS) / VC.STATE_CHUNK_EXPANSION
    )
    assert VC._state_ceiling_chars(other_track_chars=0) == base, "zero means 'no deduction'"
    other = 1_000_000
    deducted = VC._state_ceiling_chars(other_track_chars=other)
    assert deducted == int(
        (VC.STATE_CHUNK_CEILING_CHARS - VC.STATE_CEILING_MARGIN_CHARS - other)
        / VC.STATE_CHUNK_EXPANSION
    )
    assert deducted < base, "a track that holds more leaves this instance less"
    assert base - deducted == int(other / VC.STATE_CHUNK_EXPANSION)
    # a track already past the ceiling leaves nothing at all - never a negative budget
    assert VC._state_ceiling_chars(other_track_chars=VC.STATE_CHUNK_CEILING_CHARS * 2) == 0


# ---------------------------------------------------------------------------
# 2. The transaction measures the rest of the track from the chunk it read
# ---------------------------------------------------------------------------


def test_the_transaction_subtracts_everything_but_its_own_block():
    slice_ = M.TargetedStateSlice(
        chunk_digest="d",
        line_start=10,
        line_end=20,
        header_line="<VST \"x\" m 0 \"\" 1{} \"\"",
        class_hex="AA",
        ordinal=0,
        block_text="x" * 500,
        chunk_chars=1500,
    )
    fake = SimpleNamespace(_last_slice=slice_)
    assert TR.Transaction._other_track_chars(fake) == 1000


def test_an_unknown_chunk_is_not_treated_as_an_empty_track():
    """`0` is the historical behaviour; a slice without the count keeps it, never inflates it."""
    no_count = M.TargetedStateSlice(
        chunk_digest="d",
        line_start=0,
        line_end=1,
        header_line="<VST \"x\" m 0 \"\" 1{} \"\"",
        class_hex="AA",
        ordinal=0,
        block_text="x" * 500,
    )
    assert no_count.chunk_chars == 0
    assert TR.Transaction._other_track_chars(SimpleNamespace(_last_slice=no_count)) == 0
    assert TR.Transaction._other_track_chars(SimpleNamespace(_last_slice=None)) == 0
    # a block longer than the chunk it came from cannot happen; the deduction never goes negative
    odd = SimpleNamespace(chunk_chars=100, block_text="x" * 500)
    assert TR.Transaction._other_track_chars(SimpleNamespace(_last_slice=odd)) == 0


def test_the_bridge_records_the_whole_chunk_length_on_the_slice():
    """The measurement itself: whatever the host returned is what the budget is based on."""
    document = P39.live_document(P39.full_table(frames=1), P39.full_table(frames=1), P39.full_table(frames=1))
    host = host_with(document)
    chunk = host.read_state_chunk(ref())
    slice_ = host.locate_state_slice(ref(), host.fx_facts(0, 0), chunk)
    assert slice_.chunk_chars == len(chunk)
    assert slice_.chunk_chars > len(slice_.block_text), "the chunk holds more than this block"
    assert TR.Transaction._other_track_chars(SimpleNamespace(_last_slice=slice_)) == len(
        chunk
    ) - len(slice_.block_text)


# ---------------------------------------------------------------------------
# 3. The plan refuses on the track's budget, and says whose budget it is
# ---------------------------------------------------------------------------


def test_a_table_inside_the_instance_budget_is_refused_when_the_track_is_crowded():
    """The live incident in one assertion: the same plan is legal alone and refused beside a peer.

    The document is the one the P39c ceiling test uses (two heavy slots and a small one, inflating
    the third crosses the line). Here the SAME document is planned twice: alone (the historical
    behaviour, still refused - that fixture is deliberately past the limit) and beside a peer that
    holds a million chunk characters, where the refusal carries the deduction.
    """
    ceiling = VC._state_ceiling_chars()
    heavy = P39C.audio_table(samples=int(ceiling * 0.75 / 2.67))
    document = P39.live_document(heavy, heavy, P39C.audio_table(samples=512))
    host = host_with(document)
    codec = T7.provider_on_fixtures().codec()
    live = decode_live(host)
    target = P39C.audio_table(samples=65536)
    changes = {"paths": {"settings.wavetables.2": target}}

    alone = codec.plan_state_writes(live, changes)
    beside_a_peer = codec.plan_state_writes(live, changes, other_track_chars=1_000_000)
    assert [notice["reason"] for notice in alone.notices] == ["state_ceiling_reached"]
    assert [notice["reason"] for notice in beside_a_peer.notices] == ["state_ceiling_reached"]
    peer_notice = beside_a_peer.notices[0]
    assert peer_notice["other_track_chars"] == 1_000_000
    assert peer_notice["ceiling_chars"] == VC._state_ceiling_chars(1_000_000)
    assert peer_notice["ceiling_chars"] < alone.notices[0]["ceiling_chars"]
    assert "another plugin" in peer_notice["message"], peer_notice["message"]
    assert "4,189,248" in peer_notice["message"], "the refusal still names the text MEASURED readable"
    # the alone-form keeps its original remedy sentence
    assert "Free room by loading a smaller table or preset first" in alone.notices[0]["message"]


def test_a_peer_does_not_refuse_a_plan_that_still_fits_alone():
    """The deduction must be a budget, not a blanket refusal: a light plan stays planned."""
    document = P39.live_document(
        P39.full_table(frames=1), P39.full_table(frames=1), P39.full_table(frames=1)
    )
    host = host_with(document)
    codec = T7.provider_on_fixtures().codec()
    live = decode_live(host)
    changes = {"paths": {"settings.wavetables.0": P39C.audio_table(samples=4096)}}
    peery = codec.plan_state_writes(live, changes, other_track_chars=1_000_000)
    assert peery.notices == [], peery.notices
    assert peery.units, "the write is still planned"
    assert VC._document_chars(live["document"]) < VC._state_ceiling_chars(1_000_000)


def test_a_balloon_plan_uses_the_track_budget_too():
    """Both balloon planners take the deduction - the table one and the sample one."""
    import inspect as _inspect

    for name in ("_plan_balloon", "_plan_sample_balloon"):
        params = _inspect.signature(getattr(VC.VitalCodec, name)).parameters
        assert "ceiling" in params, f"{name} must accept the ceiling the schedule computed"
    code = _inspect.getsource(VC.VitalCodec.plan_state_writes)
    assert "ceiling=ceiling" in code, "the schedule hands its own ceiling to the balloon"


def test_the_provider_protocol_declares_the_track_budget():
    """The universal contract, so a provider cannot be surprised by the keyword."""
    import inspect as _inspect

    from reaper_reapy_mcp.plugins import provider as PR

    signature = _inspect.signature(PR.StateCodec.plan_state_writes)
    assert "other_track_chars" in signature.parameters
    assert signature.parameters["other_track_chars"].default == 0
