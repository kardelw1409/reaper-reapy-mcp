"""P39 (audio slots) - the table type the growl/dubstep family is built on.

MEASURED, offline and live (2026-10-17/18):

* the family survey (`_p39c_family_survey.py`): 536 dubstep/growl-ish presets, **449 with a
  custom table**, median max payload **164,353 B** - twice the P38 sample's median - and most
  of those tables are NOT `Wave Source` tables: their source component is `Audio File Source`,
  whose wave data is one `audio_file` string of base64 int16 PCM (up to ~1.4 MB of state);
* the plugin's own factory (`common/wavetable/wavetable_component_factory.cpp`) names ten
  component types, four of them SOURCES: `Wave Source`, `Line Source`, `Audio File Source`,
  `Shepard Tone Source`. The guard knew only the first, so every growl preset lost exactly the
  part that makes it growl;
* live (`probe_p39d_audio_slots.py`): an `Audio File Source` table of 1,409,808 bytes lands when
  a GENERAL balloon opens the room - the live table's first source component replaced by a stub
  `Wave Source` of K keyframes, which the plugin re-serializes as K full frames (136 stub
  keyframes bought 1,490,155 characters of state) - and the table's own write is then a shrink;
* live (`probe_p39e_audio_residual.py`, twice): the audio payload comes back with 8,856 of
  528,388 samples (1.676 %) moved by EXACTLY one step (`histogram {1: 8856}`), because the
  plugin converts int16 -> float on load and back on save. A byte comparison would report a
  landed write as a failure; a loose one would hide a wrong table.

Mock host + fixtures only: no live REAPER.
"""

from __future__ import annotations

import array
import base64
import json
import math
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
import test_p39_balloon as P39

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


# ---------------------------------------------------------------------------
# Builders: an audio-file slot, in the shape the plugin writes (`FileSource::stateToJson`)
# ---------------------------------------------------------------------------


def pcm_payload(samples: int, *, seed: int = 7, scale: int = 30000) -> str:
    """Deterministic int16 PCM, base64 (the shape `audio_file` carries)."""
    values = [int(scale * math.sin(0.01 * (index + seed))) for index in range(samples)]
    return base64.b64encode(struct.pack("<%dh" % samples, *values)).decode("ascii")


def audio_table(
    *,
    samples: int = 4096,
    name: str = "Vowel Ahh",
    version: str = "1.0.7",
    extra_components: bool = True,
    keyframes: int = 3,
) -> dict:
    """An `Audio File Source` table: one audio payload plus (optionally) modifiers."""
    components: list[dict] = [
        {
            "type": WT.AUDIO_SOURCE_COMPONENT,
            "interpolation_style": 1,
            "audio_file": pcm_payload(samples),
            "audio_sample_rate": 44100,
            "normalize_gain": False,
            "normalize_mult": True,
            "window_size": 2048.0,
            "fade_style": 1,
            "phase_style": 0,
            "random_seed": 10,
            "keyframes": [{"position": index} for index in range(keyframes)],
        }
    ]
    if extra_components:
        components.append({"type": "Frequency Filter", "interpolation_style": 1, "style": 1, "keyframes": [{}]})
    return {
        "author": "",
        "full_normalize": False,
        "name": name,
        "remove_all_dc": False,
        "version": version,
        "groups": [{"components": components}],
    }


def heavy_table() -> dict:
    """A `Wave Source` table used as the slot's CURRENT content in the tests."""
    return P39.full_table(frames=2, name="Init")


def plugin_round_trip(*transforms):
    """The measured host behaviours, in order: keyframe expansion, then the audio requant.

    The mock stores what it is given unless a test mirrors the plugin, and BOTH measured
    behaviours matter here: `WaveSourceKeyframe::jsonToState` returns a full frame per stub
    keyframe (the balloon's whole mechanism, `P39.expand_keyframes`) and the audio payload moves
    by up to one step (`ruthless_state_transform`).
    """

    def run(block_text: str) -> str:
        for transform in transforms:
            block_text = transform(block_text)
        return block_text

    return run


def ruthless_state_transform(steps: int):
    """Mirror the MEASURED plugin: move the head of every audio payload by `steps` int16 steps."""

    def transform(block_text: str) -> str:
        framing = VC.parse_block_framing(block_text)
        doc, _brace = VC.decode_document(framing["B"])
        moved = 0
        for table in ((doc.get("settings") or {}).get("wavetables") or []):
            for comp in WT.components(table):
                if comp.get("type") != WT.AUDIO_SOURCE_COMPONENT:
                    continue
                raw = comp.get("audio_file")
                if not isinstance(raw, str) or not raw:
                    continue
                data = array.array("h")
                data.frombytes(base64.b64decode(raw + "=" * (-len(raw) % 4)))
                # the live measurement moved 1.676 % of the samples, starting at index 2
                span = max(1, int(len(data) * 0.01676))
                for index in range(2, min(len(data), 2 + span)):
                    data[index] = max(-32768, min(32767, data[index] + steps))
                moved += span
                comp["audio_file"] = base64.b64encode(data.tobytes()).decode("ascii")
        new_b = VC.encode_item_b(framing["B"], doc, mode="grow")
        item_a = VC.encode_item_a(framing["A"], len(new_b))
        lines = [framing["header"], base64.b64encode(item_a).decode("ascii")]
        lines.extend(VC.wrap_lines(base64.b64encode(new_b).decode("ascii"), framing.get("b64_width") or 280))
        if framing.get("b64_artifact"):
            lines.append(framing["b64_artifact"])
        lines.extend(framing["tail_lines"])
        return "\n".join(lines)

    return transform


def run_on(host, request: dict, *, dry_run: bool):
    provider = T7.provider_on_fixtures()
    profile = provider.build_profile(dict(T7.IDENTITY))
    registry = T7.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    return T7.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=M.PluginInstanceRef(
            host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
        ),
        request=dict(request),
        token=token,
        registry=registry,
        dry_run=dry_run,
    ).run()


# ---------------------------------------------------------------------------
# 1. Recognition: an audio table IS wave data
# ---------------------------------------------------------------------------


def test_an_audio_file_slot_is_recognised_as_wave_data():
    table = audio_table(samples=1024)
    described = WT.describe(table, oscillator=1)
    assert described["source_types"] == [WT.AUDIO_SOURCE_COMPONENT]
    assert described["carries_waves"] is True
    assert described["has_wave_source"] is False, "it is not a keyframe table and must not claim to be"
    assert described["n_frames"] == 0
    assert described["audio_chars"] == len(table["groups"][0]["components"][0]["audio_file"])
    assert described["audio_samples"] == 1024
    assert "AUDIO FILE" in described["why_no_frames"]
    assert described["table_id"], "an audio table needs a content identity"


def test_the_source_types_are_the_plugins_own_four():
    assert set(WT.SOURCE_COMPONENTS) == {
        "Wave Source",
        "Audio File Source",
        "Line Source",
        "Shepard Tone Source",
    }
    for kind in WT.SOURCE_COMPONENTS:
        table = audio_table()
        component = table["groups"][0]["components"][0]
        component["type"] = kind
        assert WT.carriers(table), kind
        assert WT.carries_waves(table), kind


def test_a_modifier_only_table_carries_no_waves():
    table = {"name": "T", "groups": [{"components": [{"type": "Wave Folder"}, {"type": "Phase Shift"}]}]}
    assert WT.carriers(table) == []
    assert WT.carries_waves(table) is False
    assert WT.table_id(table) is None


def test_the_wave_source_identity_is_unchanged_by_the_audio_support():
    """A pinned id: the keyframe digest must stay byte-for-byte what P30 shipped."""
    table = P30.table(P30.saw())
    import hashlib

    digest = hashlib.sha256()
    for row in WT.keyframes(table):
        digest.update(str(row.get("position")).encode())
        digest.update(b"\x00")
        digest.update(str(row.get("wave_data")).encode())
        digest.update(b"\x1f")
    assert WT.table_id(table) == digest.hexdigest()[:20]


def test_an_audio_payload_participates_in_the_identity():
    one = audio_table(samples=512, name="A")
    other = audio_table(samples=512, name="A")
    assert WT.table_id(one) == WT.table_id(other)
    other["groups"][0]["components"][0]["audio_file"] = pcm_payload(512, seed=999)
    assert WT.table_id(one) != WT.table_id(other), "a different payload is a different table"
    # The id is the WAVE CONTENT, exactly as it has been since P30 (that is the lesson the
    # corpus taught: a table is identified by what it carries, never by a label or a graph), so
    # a modifier that differs is NOT part of it - and the verification compares the graph
    # field-wise besides the id.
    trimmed = audio_table(samples=512, name="A", extra_components=False)
    assert WT.table_id(one) == WT.table_id(trimmed)
    relabelled = audio_table(samples=512, name="A DIFFERENT NAME")
    assert WT.table_id(one) == WT.table_id(relabelled), "a label is not an identity"


def test_the_stub_comes_from_the_audio_payload_when_there_are_no_keyframes():
    table = audio_table(samples=256)
    stub = WT.frame_stub(table)
    decoded = struct.unpack("<%df" % WT.STUB_SAMPLES, base64.b64decode(stub))
    payload = base64.b64decode(table["groups"][0]["components"][0]["audio_file"])
    head = struct.unpack("<%dh" % WT.STUB_SAMPLES, payload[: 2 * WT.STUB_SAMPLES])
    assert [round(value, 6) for value in decoded] == [round(value / 32768.0, 6) for value in head]
    assert any(decoded), "the stub is the target's own opening samples, not silence"


# ---------------------------------------------------------------------------
# 2. The guard accepts it (and the balloon knows what it replaces)
# ---------------------------------------------------------------------------


def test_the_guard_accepts_an_audio_file_slot():
    live = heavy_table()
    target = audio_table(samples=65536)
    verdict = WT.check_writable(target, current=live, live_version=LIVE_VERSION)
    assert verdict["ok"] is True
    assert verdict["source_types"] == [WT.AUDIO_SOURCE_COMPONENT]
    assert verdict["carries_waves"] is True
    assert verdict["growth_in_bytes"] > 0
    assert verdict["rule"].startswith("growing: needs room")


def test_the_guard_still_refuses_a_table_with_no_source():
    with pytest.raises(InvalidValue) as exc:
        WT.check_writable(
            {"name": "T", "groups": [{"components": [{"type": "Wave Folder"}]}]},
            current=heavy_table(),
            live_version=LIVE_VERSION,
        )
    assert "no wave data" in exc.value.message
    assert "Audio File Source" in exc.value.message


def test_the_balloon_replaces_the_audio_source_and_keeps_the_modifiers():
    live = audio_table(samples=8192, name="Live Audio")
    target = audio_table(samples=4096)
    balloon = WT.balloon_table(live, target, frames=5)
    kinds = [comp["type"] for comp in WT.components(balloon)]
    assert WT.AUDIO_SOURCE_COMPONENT not in kinds, "the audio payload is what pays for the room"
    assert WT.SOURCE_COMPONENT in kinds
    assert "Frequency Filter" in kinds, "the graph's modifiers are kept as they are"
    assert balloon["name"] == "Live Audio", "it is the LIVE table that is inflated"
    assert len(WT.keyframes(balloon)) == 5
    assert balloon["groups"][0]["components"][0]["type"] == WT.SOURCE_COMPONENT
    assert WT.payload_bytes(balloon) < WT.payload_bytes(live)


def test_the_balloon_still_swaps_keyframes_when_the_source_is_a_wave_source():
    live = P39.full_table(frames=4)
    balloon = WT.balloon_table(live, P39.full_table(frames=16), frames=9)
    assert len(WT.keyframes(balloon)) == 9
    assert balloon["name"] == live["name"]


def test_the_sizing_counts_the_room_the_re_serialization_gives():
    live = audio_table(samples=8192)
    target = audio_table(samples=8192 * 32)
    sizing = WT.balloon_sizing(target, live, budget_chars=VC.BATCH_GROWTH_BUDGET)
    assert sizing["ok"] is True
    assert sizing["replaced_chars"] > 0
    assert sizing["frames"] == math.ceil(
        max(0, sizing["target_chars"] - VC.BATCH_GROWTH_BUDGET) / WT.full_frame_chars()
    )
    assert sizing["room_chars"] >= sizing["target_chars"]
    assert sizing["write_growth_chars"] < 0, "dropping the live audio pays for the balloon write"
    assert sizing["next_write_growth_chars"] <= VC.BATCH_GROWTH_BUDGET


def test_a_slot_with_nothing_to_inflate_is_a_gap_not_a_guess():
    sizing = WT.balloon_sizing(audio_table(samples=4096), {"no": "groups"}, budget_chars=24)
    assert sizing["ok"] is False
    assert "inflate" in sizing["why"]


# ---------------------------------------------------------------------------
# 3. Verification: the audio payload is a BAND, everything else is exact
# ---------------------------------------------------------------------------


def test_the_audio_band_accepts_the_measured_requantization():
    written = pcm_payload(4096, seed=3)
    moved = array.array("h")
    moved.frombytes(base64.b64decode(written))
    for index in range(2, 2 + int(len(moved) * 0.01676)):
        moved[index] += 1
    row = VC._audio_payload_band(written, base64.b64encode(moved.tobytes()).decode("ascii"))
    assert row["within_band"] is True
    assert row["exact"] is False
    assert row["max_abs_delta"] == 1
    assert row["differing"] > 0 and row["differing_fraction"] < 0.02


def test_the_audio_band_refuses_a_length_change_and_a_bigger_move():
    written = pcm_payload(4096)
    shorter = pcm_payload(2048)
    assert VC._audio_payload_band(written, shorter)["within_band"] is False
    data = array.array("h")
    data.frombytes(base64.b64decode(written))
    data[5] += 3
    assert VC._audio_payload_band(written, base64.b64encode(data.tobytes()).decode("ascii"))[
        "within_band"
    ] is False
    assert VC._audio_payload_band(written, "not base64 !!")["within_band"] is False


def test_the_table_comparison_takes_the_audio_out_and_keeps_the_rest_exact():
    written = audio_table(samples=2048)
    read_back = json.loads(json.dumps(written))
    data = array.array("h")
    data.frombytes(base64.b64decode(read_back["groups"][0]["components"][0]["audio_file"]))
    data[3] += 1
    read_back["groups"][0]["components"][0]["audio_file"] = base64.b64encode(data.tobytes()).decode("ascii")
    differing, band = VC._table_agreement(written, read_back, 1e-5)
    assert differing == [] and band and band["within_band"] is True

    read_back["groups"][0]["components"][1]["style"] = 9
    differing, band = VC._table_agreement(written, read_back, 1e-5)
    assert differing == ["groups.0.components.1.style"], "everything else stays exact"


def test_a_a_wave_source_table_has_no_audio_row():
    written = P39.full_table(frames=2)
    differing, band = VC._table_agreement(written, json.loads(json.dumps(written)), 1e-5)
    assert differing == [] and band is None


# ---------------------------------------------------------------------------
# 4. End to end: the two writes, with the plugin's own requantization mirrored
# ---------------------------------------------------------------------------


def test_the_read_ceiling_is_pinned_to_the_two_measured_states():
    """The ceiling is not a guess: it is bracketed by two live states, one readable, one not.

    MEASURED (2026-10-18, live, same track, the layer's own read path): a document of 3,048,473
    chars came back as a COMPLETE 4,083,633-character chunk (returned identically at every capacity
    at or above its length, so it was not a truncated read) - three wavetables totalling 2.9 MB
    landed and verified; a document of 3,363,963 chars (chunk 4,485,284) faulted every read with
    `access violation`. A ceiling that refuses the readable state loses capability for nothing, and
    one that allows the faulting state creates an instance no read can reach - so both bounds are
    pinned here.
    """
    readable_document = 3_048_473
    faulting_document = 3_363_963
    ceiling = VC._state_ceiling_chars()
    assert ceiling >= readable_document, "the state MEASURED readable must be allowed"
    assert ceiling < faulting_document, "the state MEASURED to fault must be refused"
    assert readable_document * VC.STATE_CHUNK_EXPANSION < VC.STATE_CHUNK_CEILING_CHARS
    assert faulting_document * VC.STATE_CHUNK_EXPANSION > VC.STATE_CHUNK_CEILING_CHARS


def test_a_balloon_that_would_pass_the_read_ceiling_is_named():
    """MEASURED (2026-10-18, live): a state whose track chunk passes ~4 MB cannot be read back.

    `GetTrackStateChunk` answered `OSError: access violation writing 0x...` for such an instance -
    a 4 MB buffer came back truncated at 3,999,999 characters while 6/9/20 MB buffers faulted - and
    the READ is what every later step (verification, rollback) needs. The planner must therefore
    refuse the write that would land there, and name it.
    """
    ceiling = VC._state_ceiling_chars()
    assert 2_000_000 < ceiling < VC.STATE_CHUNK_CEILING_CHARS, "three quarters of the text limit"
    # two slots already hold a big table each and the third is small: inflating it crosses the line
    heavy = audio_table(samples=int(ceiling * 0.75 / 2.67))
    doc = P39.live_document(heavy, heavy, audio_table(samples=512))
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(doc))])]
    )
    ref = M.PluginInstanceRef(host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY)
    codec_ = T7.provider_on_fixtures().codec()
    document = codec_.decode(
        host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    )
    target = audio_table(samples=65536)
    plan = codec_.plan_state_writes(document, {"paths": {"settings.wavetables.2": target}})
    assert [notice["reason"] for notice in plan.notices] == ["state_ceiling_reached"], plan.notices
    assert plan.units == [], "nothing is planned: the write would leave an unreadable instance"
    notice = plan.notices[0]
    assert notice["projected_chars"] > notice["ceiling_chars"]
    assert "4 MB" in notice["message"]


def test_a_balloon_under_the_ceiling_is_still_planned():
    live = P39.live_document(heavy_table(), heavy_table(), heavy_table())
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(live))])]
    )
    ref = M.PluginInstanceRef(host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY)
    codec_ = T7.provider_on_fixtures().codec()
    document = codec_.decode(
        host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    )
    plan = codec_.plan_state_writes(
        document, {"paths": {"settings.wavetables.0": audio_table(samples=65536)}}
    )
    assert [sorted((unit.payload or {}).keys()) for unit in plan.units] == [["balloon"], ["paths", "wavetable"]]
    assert plan.notices == [], plan.notices
    assert VC._document_chars(document) < VC._state_ceiling_chars()


def test_an_audio_table_lands_in_two_writes_end_to_end(tmp_path: Path):
    """The regression this round exists for: this write used to be REFUSED."""
    live = P39.live_document(heavy_table(), heavy_table(), heavy_table())
    target = audio_table(samples=65536, name="BASS HOUSE 21")
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(live))])],
        state_write_transform=plugin_round_trip(P39.expand_keyframes, ruthless_state_transform(1)),
    )
    result = run_on(host, {"state": {"settings.wavetables.0": target}}, dry_run=False)
    assert result.ok is True, result.error and result.error.to_dict()

    writes = [row for row in host.log if row[0] == "write_state_slice"]
    assert len(writes) == 2, "the balloon, then the audio table"

    rows = [row for row in result.outcome.channels["state"].rows if row.get("kind") == "wavetable"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["agrees"] is True, row["differing_fields"]
    assert row["differing_fields"] == []
    assert row["audio_payload"]["within_band"] is True
    assert row["audio_payload"]["exact"] is False, "the plugin's requant is reported, not hidden"
    # For an audio-file slot the CONTENT id cannot be expected to match: it is a digest over the
    # payload, and the plugin's int16 round trip moves up to a step of it. The row says so, and
    # the write is verified by the masked field comparison plus the band.
    assert row["table_id_agrees"] is False
    assert "expected to differ" in row["table_id_agrees_note"]
    assert row["differing_fields"] == [], "everything except the payload stays exact"
    assert row["read_back"]["source_types"] == [WT.AUDIO_SOURCE_COMPONENT]
    assert row["read_back"]["audio_samples"] == 65536

    stored = P30.decode_chunk(host)["settings"]["wavetables"][0]
    assert WT.audio_payload(stored), "the audio payload is in the instance"
    assert WT.components(stored)[0]["type"] == WT.AUDIO_SOURCE_COMPONENT

    balloons = [row for row in result.outcome.channels["state"].rows if row.get("kind") == "balloon"]
    assert len(balloons) == 1 and balloons[0]["agrees"] is True


def test_a_hard_requantization_still_fails_the_write(tmp_path: Path):
    """The band must be able to fail: a move of 3 steps is not the plugin's round trip."""
    live = P39.live_document(heavy_table(), heavy_table(), heavy_table())
    target = audio_table(samples=65536)
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(live))])],
        state_write_transform=plugin_round_trip(P39.expand_keyframes, ruthless_state_transform(3)),
    )
    result = run_on(host, {"state": {"settings.wavetables.0": target}}, dry_run=False)
    assert result.ok is False
    assert "read-back does not agree" in (result.error.message if result.error else "")


def test_an_audio_slot_is_planned_as_a_balloon_then_the_table():
    live = P39.live_document(heavy_table(), heavy_table(), heavy_table())
    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(live))])])
    codec = T7.provider_on_fixtures().codec()
    ref = M.PluginInstanceRef(host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY)
    document = codec.decode(host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref)))
    target = audio_table(samples=65536)
    plan = codec.plan_state_writes(document, {"paths": {"settings.wavetables.0": target}})
    assert [sorted((unit.payload or {}).keys()) for unit in plan.units] == [["balloon"], ["paths", "wavetable"]]
    balloon = plan.units[0].payload["balloon"]
    assert balloon["path"] == "settings.wavetables.0"
    assert balloon["room_chars"] >= balloon["target_chars"]
    assert plan.notices == []
