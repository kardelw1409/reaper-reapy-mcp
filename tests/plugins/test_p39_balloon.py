"""P39 - the state-write landing bound, and the balloon that delivers a growing table.

WHAT WAS MEASURED (2026-10-17, live Vital 1.5.5 in REAPER; probes `probe_p39_wrapper_sizes.py`,
`probe_p39_balloon.py`, `probe_p39_table_balloon.py` under
`%LOCALAPPDATA%\\reaper_reapy_mcp\\run_artifacts\\`):

1. a state write LANDS iff the new document is at most `headroom` characters longer than the
   one the host holds - `headroom = len(item B) - 8 - (the end of the JSON)`, 32 in every state
   measured. The ladder on plain string fields, with the block delta held constant: +8/+16/+24/
   +31 land, +32 lands EXACTLY on the bound, +33/+40/+64 are IGNORED and leave the state
   byte-identical. Patching item B's own size fields does not move it.
2. a table that grows the document past that bound therefore needs ROOM first. The plugin
   re-serializes EVERY wavetable keyframe as a full frame (`WaveSourceKeyframe::jsonToState`
   decodes into a frame-sized block and memcpys all of it - the same code path the live probe
   exercised with a 44-character payload that came back as 2,048 samples), so a table written
   with many stub keyframes shrinks to write and reads back LARGE. The table's own write is then
   a shrink, and a shrink always lands: measured with a 16-frame / 175,497-byte table, read back
   frame-for-frame with `table_id` equality, no NaN, and the document equal to the capture except
   the slot.

These tests pin the OFFLINE half: the headroom the framing reports, the sizing rules, the plan
shape, the guard's new verdict, and the two-write chain end to end against a host double that
mirrors the measured re-serialization. The landing itself is a live measurement.

Mock host + fixtures only: no live REAPER.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins.errors import InvalidValue
from reaper_reapy_mcp.plugins.providers.vital import codec as VC
from reaper_reapy_mcp.plugins.providers.vital import wavetables as WT

import test_p7_vital as T7
import test_p30_wavetable as P30

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


# ---------------------------------------------------------------------------
# Fixtures: a live state with FULL frames, and a preset table that grows it
# ---------------------------------------------------------------------------


def full_sine() -> list[float]:
    import math

    return [math.sin(2.0 * math.pi * i / WT.FRAME_SAMPLES) for i in range(WT.FRAME_SAMPLES)]


def full_table(*, frames: int = 1, name: str = "Init") -> dict:
    rows = [
        P30.frame(int(round(index * WT.FRAME_AXIS_MAX / frames)), full_sine())
        for index in range(frames)
    ]
    return P30.table(full_sine(), name=name, frames=rows)


def live_document(*tables: dict) -> dict:
    doc = T7.document()
    doc["synth_version"] = LIVE_VERSION
    doc["settings"]["wavetables"] = [dict(row) for row in (tables or (full_table(),) * 3)]
    return doc


def slot(doc: dict | None = None) -> MH.MockFxSlot:
    return T7.vitals_slot(block=T7.build_block(doc if doc is not None else live_document()))


def host_with(doc: dict | None = None, *, expand: bool = False) -> MH.MockHostBridge:
    return MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [slot(doc)])],
        state_write_transform=expand_keyframes if expand else None,
    )


def reference(host: MH.MockHostBridge) -> M.PluginInstanceRef:
    return M.PluginInstanceRef(
        host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
    )


def decoded(host: MH.MockHostBridge, codec):
    ref = reference(host)
    return codec.decode(host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref)))


def codec():
    return T7.provider_on_fixtures().codec()


def run_on(host: MH.MockHostBridge, request: dict, *, dry_run: bool):
    provider = T7.provider_on_fixtures()
    profile = provider.build_profile(dict(T7.IDENTITY))
    registry = T7.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    return T7.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=reference(host),
        request=dict(request),
        token=token,
        registry=registry,
        dry_run=dry_run,
    ).run()


def expand_keyframes(block_text: str) -> str:
    """Mirror the MEASURED plugin: every keyframe re-serializes as a full frame.

    `WaveSourceKeyframe::jsonToState` (source, 1.0.6 era, and measured live on 1.5.5) decodes
    `wave_data` into a block sized for one frame and memcpys the whole frame, so a short written
    payload reads back padded. That is the whole balloon mechanism, and a host double that
    ignores it could not exercise the two-write chain at all.
    """
    framing = VC.parse_block_framing(block_text)
    doc, _brace = VC.decode_document(framing["B"])
    for table in ((doc.get("settings") or {}).get("wavetables") or []):
        for component in WT.components(table):
            if component.get("type") != WT.SOURCE_COMPONENT:
                continue
            for row in component.get("keyframes") or []:
                raw = row.get("wave_data")
                values: list[float] = []
                if isinstance(raw, str) and raw:
                    decoded_bytes = base64.b64decode(raw + "=" * (-len(raw) % 4))
                    count = len(decoded_bytes) // 4
                    if count:
                        values = list(struct.unpack("<%df" % count, decoded_bytes[: count * 4]))
                padded = (values + [0.0] * WT.FRAME_SAMPLES)[: WT.FRAME_SAMPLES]
                row["wave_data"] = base64.b64encode(
                    struct.pack("<%df" % WT.FRAME_SAMPLES, *padded)
                ).decode("ascii")
    new_b = VC.encode_item_b(framing["B"], doc, mode="grow")
    item_a = VC.encode_item_a(framing["A"], len(new_b))
    lines = [framing["header"], base64.b64encode(item_a).decode("ascii")]
    lines.extend(VC.wrap_lines(base64.b64encode(new_b).decode("ascii"), framing.get("b64_width") or 280))
    if framing.get("b64_artifact"):
        lines.append(framing["b64_artifact"])
    lines.extend(framing["tail_lines"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. The headroom the framing reports
# ---------------------------------------------------------------------------


def test_the_framing_reports_the_measured_headroom():
    """`len(item B) - 8 - the end of the document`, computed independently here."""
    codec_ = codec()
    host = host_with()
    ref = reference(host)
    slice_ = host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    view = codec_.decode(slice_)
    framing = VC.parse_block_framing(slice_.block_text)
    B = framing["B"]
    _doc, brace = VC.decode_document(B)
    _obj, consumed = VC._decode_at(B, brace)
    assert view["framing"]["headroom_bytes"] == len(B) - VC.HOST_TRAILER_BYTES - (brace + consumed)
    assert view["framing"]["headroom_bytes"] == VC.STATE_HEADROOM_BYTES, (
        "the fixture block carries the recorded layout, so the measured headroom must come out"
    )


def test_the_budget_is_the_measured_headroom_minus_its_margin():
    """The constants carry their derivation (the P38 numbers were a mismeasurement of it)."""
    assert VC.BATCH_GROWTH_BUDGET == VC.STATE_HEADROOM_BYTES - VC.HEADROOM_MARGIN_BYTES
    assert VC.SINGLE_PATH_GROWTH_LIMIT == VC.BATCH_GROWTH_BUDGET
    assert 0 < VC.HEADROOM_MARGIN_BYTES < VC.STATE_HEADROOM_BYTES
    assert VC.BATCH_GROWTH_BUDGET * VC.GROWTH_ESTIMATE_TO_REAL <= VC.MEASURED_LANDING_BAND


def test_the_document_growth_budget_uses_the_live_headroom():
    """A framing with a smaller headroom lowers the budget; an absent one falls back."""
    assert VC._growth_budget({"framing": {"headroom_bytes": 20}}) == 12
    assert VC._growth_budget({"framing": {}}) == VC.BATCH_GROWTH_BUDGET
    assert VC._growth_budget({}) == VC.BATCH_GROWTH_BUDGET
    assert VC._growth_budget({"framing": {"headroom_bytes": 4}}) == 0


# ---------------------------------------------------------------------------
# 2. The sizing rules
# ---------------------------------------------------------------------------


def test_one_full_frame_per_stub_keyframe_is_what_buys_the_room():
    live = full_table()
    target = full_table(frames=4)
    sizing = WT.balloon_sizing(target, live, budget_chars=VC.BATCH_GROWTH_BUDGET)
    assert sizing["ok"] is True
    assert sizing["frame_chars"] == WT.full_frame_chars()
    assert sizing["room_chars"] >= sizing["target_chars"]
    assert sizing["write_growth_chars"] <= VC.BATCH_GROWTH_BUDGET
    assert sizing["next_write_growth_chars"] <= VC.BATCH_GROWTH_BUDGET
    # the room is the frames the balloon writes, plus the live table's non-keyframe part
    assert sizing["frames"] >= 4, "one full frame per stub keyframe, at least as many as the target"


def test_the_balloon_write_is_a_shrink_over_the_live_table():
    """It has to land itself: the stub rows are paid for by the live table's own frames."""
    live = full_table(frames=8)
    target = full_table(frames=16)
    sizing = WT.balloon_sizing(target, live, budget_chars=VC.BATCH_GROWTH_BUDGET)
    assert sizing["ok"] is True
    assert sizing["frames"] >= 16
    assert sizing["write_growth_chars"] <= 0
    table = WT.balloon_table(live, target, frames=sizing["frames"], stub=None)
    assert len(WT.frame_positions(table)) == sizing["frames"]
    assert sorted(set(WT.frame_positions(table))) == WT.frame_positions(table), "positions ascend"


def test_a_stub_table_keeps_the_live_structure_and_the_targets_own_samples():
    live = full_table(name="Live Table")
    target = full_table(frames=2, name="Target")
    table = WT.balloon_table(live, target, frames=2)
    assert table["name"] == "Live Table", "the balloon is the LIVE table, inflated"
    assert WT.source_component(table)["keyframes"][0] == {
        **WT.source_component(live)["keyframes"][0],
        "wave_data": WT.frame_stub(target),
    }, "the stub is the target's own first samples, on the live keyframe template"
    decoded_stub = base64.b64decode(WT.frame_stub(target))
    assert len(decoded_stub) == 4 * WT.STUB_SAMPLES
    first = struct.unpack("<%df" % WT.STUB_SAMPLES, decoded_stub)
    written = struct.unpack("<%df" % WT.STUB_SAMPLES, base64.b64decode(WT.keyframes(target)[0]["wave_data"])[: 4 * WT.STUB_SAMPLES])
    assert first == pytest.approx(written)


def test_a_table_too_large_for_the_axis_is_refused_as_a_gap():
    """Beyond 256 stub keyframes there is no room to open, and that is NAMED, not guessed."""
    live = full_table()
    target = full_table(frames=WT.FRAME_AXIS_MAX)  # 256 full frames: more than the axis can cover
    sizing = WT.balloon_sizing(target, live, budget_chars=VC.BATCH_GROWTH_BUDGET)
    assert sizing["ok"] is False, sizing
    assert sizing["frames"] > WT.FRAME_AXIS_MAX
    assert "axis" in sizing["why"]
    plan = codec().plan_state_writes(
        decoded(host_with(), codec()), {"paths": {"settings.wavetables.0": target}}
    )
    assert plan.units == []
    assert [notice["reason"] for notice in plan.notices] == ["no_room_for_growth"]
    assert plan.notices[0]["growth_chars"] > VC.BATCH_GROWTH_BUDGET


# ---------------------------------------------------------------------------
# 3. The plan: a balloon first, then the table
# ---------------------------------------------------------------------------


def test_a_growing_table_is_planned_as_a_balloon_then_the_table():
    live = live_document()
    target = full_table(frames=4)
    plan = codec().plan_state_writes(decoded(host_with(), codec()), {"paths": {"settings.wavetables.0": target}})
    assert [sorted((unit.payload or {}).keys()) for unit in plan.units] == [["balloon"], ["paths", "wavetable"]]
    balloon = plan.units[0].payload["balloon"]
    assert balloon["path"] == "settings.wavetables.0"
    assert balloon["frames"] >= 4 and balloon["room_chars"] >= balloon["target_chars"]
    assert balloon["budget_chars"] == VC.BATCH_GROWTH_BUDGET
    assert plan.notices == []
    assert [unit.order_index for unit in plan.units] == [0, 1]


def test_a_non_growing_table_needs_no_balloon():
    plan = codec().plan_state_writes(
        decoded(host_with(), codec()), {"paths": {"settings.wavetables.0": full_table()}}
    )
    assert [sorted((unit.payload or {}).keys()) for unit in plan.units] == [["paths", "wavetable"]]
    assert plan.notices == []


# ---------------------------------------------------------------------------
# 4. The guard: the value is judged, no longer its size
# ---------------------------------------------------------------------------


def test_the_guard_carries_a_growing_table_and_says_how():
    live = full_table()
    target = full_table(frames=4)
    verdict = WT.check_writable(target, current=live, live_version=LIVE_VERSION)
    assert verdict["ok"] is True
    assert verdict["growth_in_bytes"] > 0
    assert verdict["rule"].startswith("growing: needs room")
    assert "P39" in verdict["evidence"] or "balloon" in verdict["evidence"]


def test_the_guard_still_refuses_what_no_measurement_supports():
    live = full_table()
    processing_only = {
        "name": "T",
        "version": LIVE_VERSION,
        "groups": [{"components": [{"type": "Wave Folder"}]}],
    }
    with pytest.raises(InvalidValue) as exc:
        WT.check_writable(processing_only, current=live, live_version=LIVE_VERSION)
    assert "Wave Source" in exc.value.message
    with pytest.raises(InvalidValue) as exc:
        WT.check_writable(full_table(), current=live, live_version=None)
    assert "synth_version" in exc.value.message
    with pytest.raises(InvalidValue) as exc:
        WT.check_writable("not a table", current=live, live_version=LIVE_VERSION)
    assert "table object" in exc.value.message


def test_the_host_abort_evidence_records_what_superseded_it():
    """The P30 measurement stays recorded, and the entry names what replaced its reading."""
    assert WT.HOST_ABORT_EVIDENCE["established"]
    assert "P39" in WT.HOST_ABORT_EVIDENCE["superseded_by"]
    assert WT.HOST_ABORT_EVIDENCE["hypothesis"], "the superseded hypothesis is kept as history"
    assert WT.VERIFIED_PAYLOAD_BYTES == 11_159


# ---------------------------------------------------------------------------
# 5. The two-write chain, end to end
# ---------------------------------------------------------------------------


def test_a_growing_table_lands_in_two_writes_end_to_end(tmp_path: Path):
    """The regression: this write used to be REFUSED (no path existed to land it)."""
    preset = P30.preset_file(tmp_path, [full_table(frames=4, name="Target")])
    host = host_with(expand=True)
    result = run_on(host, {"wavetable": [{"oscillator": 1, "from_preset": preset}]}, dry_run=False)
    assert result.ok is True, result.error and result.error.to_dict()

    writes = [row for row in host.log if row[0] == "write_state_slice"]
    assert len(writes) == 2, "the balloon and the table, in that order"

    rows = [row for row in result.outcome.channels["state"].rows if row.get("kind") == "wavetable"]
    assert len(rows) == 1 and rows[0]["agrees"] is True, rows
    assert rows[0]["table_id_read_back"] == rows[0]["table_id_written"]
    assert rows[0]["differing_fields"] == []
    assert rows[0]["stamped_version_to"] == LIVE_VERSION

    balloons = [row for row in result.outcome.channels["state"].rows if row.get("kind") == "balloon"]
    assert len(balloons) == 1, "the balloon is its own verified unit"
    assert balloons[0]["agrees"] is True
    assert balloons[0]["room_chars"] >= balloons[0]["target_chars"]
    assert balloons[0]["frames_written"] == balloons[0]["frames_read_back"]

    stored = WT.describe(P30.decode_chunk(host)["settings"]["wavetables"][0])
    assert stored["table_id"] == WT.table_id(full_table(frames=4, name="Target"))
    assert stored["n_frames"] == 4
    assert stored["total_samples"] == 4 * WT.FRAME_SAMPLES


def test_the_balloon_is_verified_by_room_and_can_fail(tmp_path: Path):
    """A read-back without the room is a failure, not a silent pass."""
    host = host_with()
    ref = reference(host)
    live_slice = host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    plan = codec().plan_state_writes(
        codec().decode(live_slice), {"paths": {"settings.wavetables.0": full_table(frames=4)}}
    )
    balloon_unit = plan.units[0]
    policy = T7.provider_on_fixtures().verification_policy()

    unexpanded = codec().verify(codec().decode(live_slice), balloon_unit, policy)
    assert unexpanded.ok is False, "the un-expanded read-back has no room: the balloon did not happen"
    assert unexpanded.mismatched[0]["kind"] == "balloon"
    assert unexpanded.mismatched[0]["room_chars"] < unexpanded.mismatched[0]["target_chars"]

    # the SAME unit, written to a host that mirrors the plugin's re-serialization
    expanded = host_with(expand=True)
    expanded_ref = reference(expanded)
    expanded_slice = expanded.locate_state_slice(
        expanded_ref, expanded.fx_facts(0, 0), expanded.read_state_chunk(expanded_ref)
    )
    written = codec().encode_slice(codec().decode(expanded_slice), balloon_unit, expanded_slice)
    expanded.write_state_slice(
        expanded_ref, expanded_slice, written, timing=T7.provider_on_fixtures().verification_policy().timing
    )
    after = codec().decode(
        expanded.locate_state_slice(
            expanded_ref, expanded.fx_facts(0, 0), expanded.read_state_chunk(expanded_ref)
        )
    )
    report = codec().verify(after, balloon_unit, policy)
    assert report.ok is True, report.mismatched
    assert report.rows[0]["room_chars"] >= report.rows[0]["target_chars"]


def test_a_growing_table_through_the_raw_state_path_lands_too(tmp_path: Path):
    host = host_with(expand=True)
    target = full_table(frames=4, name="Target")
    result = run_on(host, {"state": {"settings.wavetables.0": target}}, dry_run=False)
    assert result.ok is True, result.error and result.error.to_dict()
    stored = P30.decode_chunk(host)["settings"]["wavetables"][0]
    assert WT.table_id(stored) == WT.table_id(target)
    assert len(WT.frame_positions(stored)) == 4
