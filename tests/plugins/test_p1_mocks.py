"""P1-3 / P1-4: mock host fixtures, byte fidelity, injection modes, codec B.

These tests are the instrument for P2: they prove the mock can produce the
states and failures the transaction matrix needs, and that a multi-block chunk
is handled byte-accurately.
"""

from __future__ import annotations

import pytest

import fixtures as F
import mock_host as MH
from mock_codec_b import B_HEADER, CodecB, b_document, make_b_block_text
from mock_provider import DemoCodec, MockProvider

from reaper_reapy_mcp.plugins.errors import (
    NotConnected,
    StateConflict,
    TargetSliceAmbiguous,
    Unreadable,
    Wedged,
)
from reaper_reapy_mcp.plugins.host.contract import HostBridge
from reaper_reapy_mcp.plugins.model import (
    PluginInstanceRef,
    StateWriteUnit,
    TimingProfile,
)
from reaper_reapy_mcp.plugins.provider import PluginProvider

THIRD_CLASS = "22223333444455556666777788889999"


def _state_unit(payload: dict) -> StateWriteUnit:
    return StateWriteUnit(order_index=0, kind="state", payload=payload)


def demo_ref(track_index: int = 0, fx_index: int = 1) -> PluginInstanceRef:
    return PluginInstanceRef(
        host="reaper",
        location={"track_index": track_index, "fx_index": fx_index},
        provider="demo",
        identity=F.demo_facts(),
    )


def three_slot_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    target = MH.MockFxSlot()
    third = MH.MockFxSlot(
        name="VST3i: Third Synth",
        module="third.vst3",
        class_id=THIRD_CLASS,
        block=F.foreign_block_text(
            name="VST3i: Third Synth", module="third.vst3", class_hex=THIRD_CLASS
        ),
        params={"0:9100": 0.2},
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, target, third])])


# ---------------------------------------------------------------------------
# Protocol conformance (replaces the P0 xfail stub)
# ---------------------------------------------------------------------------


def test_mocks_conform_to_the_declared_protocols():
    assert isinstance(MH.MockHostBridge(), HostBridge)
    provider = MockProvider()
    assert isinstance(provider, PluginProvider)
    for attribute in ("id", "implementation_revision", "codec", "write_order", "capture_scope"):
        assert hasattr(provider, attribute)


def test_mock_provider_profile_builds_and_validates():
    from reaper_reapy_mcp.plugins.profile import validate_profile

    provider = MockProvider()
    profile = provider.build_profile(provider.identify(_facts()))
    result = validate_profile(profile)
    assert result.ok, result.problems


def _facts(**overrides):
    payload = dict(F.demo_facts())
    payload.update(overrides)
    from reaper_reapy_mcp.plugins.model import FxHostFacts

    return FxHostFacts(**payload)


# ---------------------------------------------------------------------------
# P1-3: multi-block chunks, spans, byte fidelity
# ---------------------------------------------------------------------------


def test_locate_selects_the_identity_matching_block_not_the_first():
    host = three_slot_host()
    chunk = host.read_state_chunk(demo_ref())
    parsed = MH.parse_blocks(chunk)
    assert [block["name"] for block in parsed] == [
        F.FOREIGN_NAME,
        F.DEMO_NAME,
        "VST3i: Third Synth",
    ]
    slice_ = host.locate_state_slice(demo_ref(), _facts(), chunk)
    assert slice_.ordinal == 0
    assert slice_.class_hex == F.DEMO_CLASS
    assert slice_.block_text == host.slot(0, 1).block
    assert F.decode_block_document(slice_.block_text)["settings"]["mode"] == "poly"
    # the foreign payload is NOT our document: decoding it fails loudly
    with pytest.raises(ValueError):
        F.decode_block_document(host.slot(0, 0).block)


def test_span_covers_only_the_selected_block():
    host = three_slot_host()
    chunk = host.read_state_chunk(demo_ref())
    slice_ = host.locate_state_slice(demo_ref(), _facts(), chunk)
    before, after = F.split_outside(chunk, slice_.line_start, slice_.line_end)
    assert host.slot(0, 0).block in before
    assert host.slot(0, 2).block in after
    assert F.DEMO_NAME in before or F.DEMO_NAME in slice_.header_line


def test_two_identical_instances_require_the_ordinal():
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [MH.MockFxSlot(), MH.MockFxSlot()])]
    )
    chunk = host.read_state_chunk(demo_ref(fx_index=1))
    with pytest.raises(TargetSliceAmbiguous):
        MH.locate_block_span(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE)
    slice_ = host.locate_state_slice(demo_ref(fx_index=1), _facts(), chunk)
    assert slice_.ordinal == 1
    first = host.locate_state_slice(demo_ref(fx_index=0), _facts(), chunk)
    assert first.ordinal == 0
    with pytest.raises(TargetSliceAmbiguous):
        MH.locate_block_span(
            chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=0, matching_fx_count=3
        )
    with pytest.raises(TargetSliceAmbiguous):
        MH.locate_block_span(chunk, class_hex=F.DEMO_CLASS, module=F.DEMO_MODULE, ordinal=2)


def test_missing_identity_evidence_refuses_instead_of_guessing():
    host = three_slot_host()
    chunk = host.read_state_chunk(demo_ref())
    with pytest.raises(TargetSliceAmbiguous):
        MH.locate_block_span(chunk, class_hex=None, module=None)
    with pytest.raises(Unreadable):
        MH.locate_block_span(chunk, class_hex="FF" * 16, module="absent.vst3")


def test_splice_keeps_every_other_byte_and_updates_only_the_target():
    host = three_slot_host()
    ref = demo_ref()
    codec = DemoCodec()
    chunk = host.read_state_chunk(ref)
    slice_ = host.locate_state_slice(ref, _facts(), chunk)
    unit_new = codec.encode_slice(
        codec.decode(slice_),
        _state_unit({"paths": {"settings.mode": "mono"}}),
        slice_,
    )
    host.write_state_slice(ref, slice_, unit_new, timing=TimingProfile(settle_s=0.4))
    after_chunk = host.read_state_chunk(ref)
    before_outside, after_outside = F.split_outside(chunk, slice_.line_start, slice_.line_end)
    new_before, new_after = F.split_outside(after_chunk, slice_.line_start, slice_.line_end)
    assert (before_outside, after_outside) == (new_before, new_after)
    assert F.decode_block_document(host.slot(0, 1).block)["settings"]["mode"] == "mono"
    assert host.slot(0, 0).block == F.foreign_block_text()
    assert host.sleeps == [0.4]


def test_stale_slice_is_refused():
    host = three_slot_host()
    ref = demo_ref()
    chunk = host.read_state_chunk(ref)
    slice_ = host.locate_state_slice(ref, _facts(), chunk)
    host.external_set_document(
        {**F.demo_document(), "version": 2}, track_index=0, fx_index=1
    )
    with pytest.raises(StateConflict):
        host.write_state_slice(ref, slice_, slice_, timing=TimingProfile())


def test_re_read_slice_reflects_the_new_document():
    host = three_slot_host()
    ref = demo_ref()
    codec = DemoCodec()
    chunk = host.read_state_chunk(ref)
    slice_ = host.locate_state_slice(ref, _facts(), chunk)
    new_block = codec.encode_slice(
        codec.decode(slice_), _state_unit({"paths": {"settings.mode": "mono"}}), slice_
    )
    host.write_state_slice(ref, slice_, new_block, timing=TimingProfile())
    fresh = host.re_read_slice(ref, _facts(), None)
    assert codec.decode(fresh)["settings"]["mode"] == "mono"


# ---------------------------------------------------------------------------
# P1-4: injection modes are visible and complete
# ---------------------------------------------------------------------------


def test_store_nothing_write_reports_api_success_and_stores_nothing():
    host = three_slot_host()
    ref = demo_ref()
    before = dict(host.slot(0, 1).params)
    host.inject("write_params", "store-nothing")
    attempt = host.write_params(ref, [("3:1003", 0.9)])
    assert attempt.returned is True  # the API said success ...
    assert host.slot(0, 1).params == before  # ... and nothing was stored


def test_store_nothing_state_write_reports_success_without_changing_the_chunk():
    host = three_slot_host()
    ref = demo_ref()
    codec = DemoCodec()
    chunk = host.read_state_chunk(ref)
    slice_ = host.locate_state_slice(ref, _facts(), chunk)
    new_block = codec.encode_slice(
        codec.decode(slice_), _state_unit({"paths": {"settings.mode": "mono"}}), slice_
    )
    host.inject("write_state_slice", "store-nothing")
    attempt = host.write_state_slice(ref, slice_, new_block, timing=TimingProfile())
    assert attempt.returned is True
    assert host.read_state_chunk(ref) == chunk


def test_drop_after_return_applies_the_write_and_then_loses_the_outcome():
    host = three_slot_host()
    ref = demo_ref()
    host.inject("write_params", "drop-after-return")
    with pytest.raises(NotConnected):
        host.write_params(ref, [("3:1003", 0.9)])
    assert host.slot(0, 1).params["3:1003"] == 0.9  # the effect happened


def test_wedge_and_raise_injections():
    host = three_slot_host()
    host.inject("read_state_chunk", "wedge")
    with pytest.raises(Wedged):
        host.read_state_chunk(demo_ref())
    host.inject("fx_facts", "raise", error=Unreadable("injected"))
    with pytest.raises(Unreadable):
        host.fx_facts(0, 1)


def test_param_read_is_one_batched_call_in_order():
    host = three_slot_host()
    idents = host.param_idents(demo_ref(), list(range(8)))
    values = host.read_params(demo_ref(), list(range(8)))
    assert host.count("read_params") == 1
    assert len(values) == 8
    assert values[3] == host.slot(0, 1).params[idents[3]]


def test_a_short_read_is_detectable_as_nothing_read():
    host = three_slot_host()
    host.inject("read_params", "store-nothing")
    assert host.read_params(demo_ref(), list(range(8))) == []


def test_external_mutation_and_replacement_hooks_change_the_facts():
    host = three_slot_host()
    host.external_set_param("3:1003", 0.99, fx_index=1)
    assert host.slot(0, 1).params["3:1003"] == 0.99
    host.external_replace_slot(class_id="00000000000000000000000000000001", fx_index=1)
    assert host.fx_facts(0, 1).class_id == "00000000000000000000000000000001"
    host.external_move_slot(1, 2)
    assert host.fx_facts(0, 2).class_id == "00000000000000000000000000000001"


# ---------------------------------------------------------------------------
# Codec B: different shape, different batching (AUD-004 groundwork)
# ---------------------------------------------------------------------------


def test_codec_b_round_trips_its_own_shape():
    codec = CodecB()
    doc = b_document()
    block = make_b_block_text(F.flat_paths(doc), header=B_HEADER)
    slice_ = MH.locate_block_span(
        F.track_chunk([block]), class_hex="FFEEDDCCBBAA99887766554433221100", module="demo_b.vst3"
    )
    decoded = codec.decode(slice_)
    assert F.flat_paths(decoded) == F.flat_paths(doc)
    assert isinstance(decoded["flags"]["enabled"], bool)


def test_codec_b_batches_by_section_where_codec_a_batches_once():
    changes = {"paths": {"tree.engine.gain": 0.4, "flags.bypass": True}}
    codec_a_plan = DemoCodec().plan_state_writes({}, changes)
    codec_b_plan = CodecB().plan_state_writes({}, changes)
    assert len(codec_a_plan.units) == 1
    assert len(codec_b_plan.units) == 2
    assert [unit.payload["section"] for unit in codec_b_plan.units] == ["flags", "tree"]


def test_codec_b_snapshot_restore_round_trip():
    codec = CodecB()
    doc = b_document()
    snap = codec.snapshot_document(doc)
    changed = b_document()
    changed["tree"]["engine"]["gain"] = 0.9
    changes = codec.restore_changes(changed, snap)
    assert changes["paths"]["tree.engine.gain"] == 0.3
