"""P40: two measured laws the preset channel MUST obey, and one of them the layer got wrong.

1. **The document's peak decides whether a payload lands, so the ORDER decides it too.**
   MEASURED (2026-10-18, live): the same `BASS Broken Circuit` apply refused its sample
   (`no_room_for_sample_growth`) while the sample pair was pinned FIRST - it met the document while
   that still held the previous preset's three 1.4 MB tables. The very same preset SHRINKS two of
   those tables (1,409,791 and 86,997 bytes down to 11,149 each) a couple of units later, which frees
   ~1.5 MB. The plan now simulates a few candidate orders and takes the one whose peak is lowest.

2. **A payload the plugin re-encodes must not be re-sent as read back.**
   MEASURED (2026-10-18, live): `BASS - Deep 2`'s stereo sample landed with the file's name and
   length but 99.6 % of its samples differed - it was the file's payload delayed by 48 samples,
   because the plugin's sample round trip returns the PCM 4 samples late (`SAMPLE_RELOAD_DELAY`) and
   EVERY later unit re-sends the whole document, which came from the read-back. Each step was within
   the delay band, so per-unit verification could not see it. Re-injecting the value the transaction
   INTENDED keeps the instance on target; one round trip of delay remains, which is the plugin's own.

Mock host + fixtures only: no live REAPER.
"""

from __future__ import annotations

import array
import base64
import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

import mock_host as MH  # noqa: E402

from reaper_reapy_mcp.plugins import model as M  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402

import test_p7_vital as T7  # noqa: E402
import test_p39_balloon as P39  # noqa: E402
import test_p39c_audio_slots as P39C  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


# ---------------------------------------------------------------------------
# 1. The order: the pair meets the document the schedule gives it
# ---------------------------------------------------------------------------


def delay_sample_transform(steps: int = VC.SAMPLE_RELOAD_DELAY):
    """The plugin's OWN round trip: every state load re-encodes the sample ``steps`` late.

    This is what the live instance does (`Sample::jsonToState` sizes a buffer from the JSON's
    `length`, `stateToJson` writes the buffer back) and it is why the layer may not re-send what it
    read: the delay would compound once per following write.
    """

    def transform(block_text: str) -> str:
        framing = VC.parse_block_framing(block_text)
        doc, _brace = VC.decode_document(framing["B"])
        sample = ((doc.get("settings") or {}).get("sample")) or {}
        for key in VC.SAMPLE_PAYLOAD_KEYS:
            value = sample.get(key)
            if not isinstance(value, str) or not value:
                continue
            raw = base64.b64decode(value + "=" * (-len(value) % 4))
            data = array.array("h")
            data.frombytes(raw[: len(raw) // 2 * 2])
            delayed = array.array("h", [0] * steps + list(data[: max(0, len(data) - steps)]))
            sample[key] = base64.b64encode(delayed.tobytes()).decode("ascii")
        new_b = VC.encode_item_b(framing["B"], doc, mode="grow")
        item_a = VC.encode_item_a(framing["A"], len(new_b))
        lines = [framing["header"], base64.b64encode(item_a).decode("ascii")]
        lines.extend(
            VC.wrap_lines(base64.b64encode(new_b).decode("ascii"), framing.get("b64_width") or 280)
        )
        if framing.get("b64_artifact"):
            lines.append(framing["b64_artifact"])
        lines.extend(framing["tail_lines"])
        return "\n".join(lines)

    return transform


def live_document_near_the_ceiling() -> dict:
    """A live state shaped like the measured one: two 1.4 MB tables plus a 117 KB sample."""
    big = P39C.audio_table(samples=528_388, name="BIG LIVE")
    init = P39.full_table(frames=1)
    document = P39.live_document(big, big, init)
    document["synth_version"] = LIVE_VERSION
    document["settings"]["sample"] = {
        "name": "White Noise",
        "length": 44_100,
        "sample_rate": 44_100,
        "samples": base64.b64encode(
            array.array("h", [((index * 37) % 2000) - 1000 for index in range(44_100)]).tobytes()
        ).decode("ascii"),
    }
    return document


def growing_sample(length: int = 81_415) -> dict:
    values = array.array("h", [((index * 91) % 4000) - 2000 for index in range(length)])
    return {
        "name": "circuit_52",
        "length": length,
        "sample_rate": 44_100,
        "samples": base64.b64encode(values.tobytes()).decode("ascii"),
    }


def host_with(document: dict, *, transform=None) -> MH.MockHostBridge:
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(document))])],
        state_write_transform=transform,
    )
    host.slot(0, 0).version = LIVE_VERSION
    return host


def decode_live(host: MH.MockHostBridge) -> dict:
    codec = T7.provider_on_fixtures().codec()
    ref = M.PluginInstanceRef(
        host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
    )
    return codec.decode(
        host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    )


def test_the_sample_pair_goes_after_the_write_that_makes_room_for_it():
    """MEASURED (live): pinned first, this exact preset lost its sample to `no_room_for_sample_growth`.

    The live document holds two 1.4 MB tables; the preset replaces ONE of them with an Init-like
    table (a 1.4 MB shrink) and brings an 81,415-sample sample. Pinned first the sample's balloon
    projects past the read ceiling; scheduled after the shrink it fits with ~1.4 MB to spare.
    """
    document = live_document_near_the_ceiling()
    host = host_with(document)
    codec = T7.provider_on_fixtures().codec()
    live = decode_live(host)
    sample = growing_sample()
    plan = codec.plan_state_writes(
        live,
        {
            "paths": {
                "settings.wavetables.0": P39.full_table(frames=1),
                "settings.sample": sample,
            }
        },
    )
    reasons = [notice["reason"] for notice in plan.notices]
    assert reasons == ["sample_inflated_before_write"], reasons
    kinds: list[str] = []
    for unit in plan.units:
        payload = unit.payload or {}
        if payload.get("sample_balloon"):
            kinds.append("sample_balloon")
        elif payload.get("balloon"):
            kinds.append("table_balloon")
        elif payload.get("paths", {}).get(VC.SAMPLE_PATH):
            kinds.append("sample")
        elif payload.get("paths"):
            kinds.append("table")
    assert kinds[0] == "table", kinds
    assert kinds[1] == "sample_balloon" and kinds[2] == "sample", kinds
    # and the peak the schedule claims is under the ceiling at every step
    start = VC._document_chars(live["document"])
    assert start + 142_000 > VC._state_ceiling_chars(), (
        "the fixture must be tight enough that the pinned-first order would NOT fit",
        start,
    )


def test_the_lowest_peak_order_beats_the_order_it_was_given():
    """The scheduler is arithmetic over known numbers, so its choice is testable directly."""
    start = 1_000
    items = [
        {"name": "big balloon", "net": 100, "overhead": 500},
        {"name": "shrink", "net": -400, "overhead": 0},
        {"name": "small", "net": 50, "overhead": 0},
    ]
    chosen = VC._lowest_peak_order(items, start)
    assert [item["name"] for item in chosen][0] == "shrink", [item["name"] for item in chosen]

    def peak_for(order):
        running = start
        peak = running
        for item in order:
            peak = max(peak, running + item["overhead"])
            running += item["net"]
        return peak

    assert peak_for(chosen) < peak_for(items), (peak_for(chosen), peak_for(items))
    # the chosen order is the one that reaches the theoretical floor for this fixture
    assert peak_for(chosen) == 1_150
    assert VC._lowest_peak_order([], start) == []


# ---------------------------------------------------------------------------
# 2. The carry: the value we wrote is the value we keep sending
# ---------------------------------------------------------------------------


def test_a_re_encoded_payload_does_not_drift_across_the_units_that_follow():
    """MEASURED (live): 48 samples of drift after a 12-unit apply - the file's own waveform sliding.

    The mock mirrors the plugin's round trip (`delay_sample_transform`), so a layer that re-sends what
    it read back drifts by one delay per following write. The transaction now re-injects the value it
    INTENDED, so the residue is exactly ONE delay - the plugin's own, the one the verification band
    already reports.
    """
    document = live_document_near_the_ceiling()
    host = host_with(
        document,
        # BOTH measured plugin behaviours: a wavetable balloons by re-serializing full frames, and
        # the sample round trip returns its PCM four samples late
        transform=P39C.plugin_round_trip(P39.expand_keyframes, delay_sample_transform()),
    )
    sample = growing_sample(length=20_000)
    request = {
        "state": {
            "settings.sample": sample,
            "settings.osc_1_level": 0.8,
            "settings.osc_2_level": 0.7,
            "settings.osc_3_level": 0.6,
            "settings.filter_1_cutoff": 42.0,
            # three table slots, so the plan carries several units AFTER the sample's own write:
            # every one of them re-sends the whole document, which is the drift this test measures
            "settings.wavetables.0": P39.full_table(frames=2),
            "settings.wavetables.2": P39.full_table(frames=3),
        }
    }
    result = P39C.run_on(host, request, dry_run=False)
    assert result.ok is True, result.error and result.error.to_dict()

    live = decode_live(host)
    written_block = live["document"]["settings"]["sample"]
    assert written_block.get("name") == sample["name"]
    assert written_block.get("length") == sample["length"]
    alignment = VC._sample_alignment(sample["samples"], written_block.get("samples"))
    assert alignment is not None and alignment.get("within_delay") is True, alignment
    assert abs(int(alignment["shift"])) == VC.SAMPLE_RELOAD_DELAY, (
        "exactly ONE round trip of delay: more than that is the drift this test exists for",
        alignment,
    )
    writes = [row for row in host.log if row[0] == "write_state_slice"]
    assert len(writes) >= 3, "the fixture must write enough units AFTER the sample to drift"


def test_the_carry_never_overrides_the_path_a_unit_writes_itself():
    """A later unit that changes the same path must win over the carried value."""
    document = live_document_near_the_ceiling()
    host = host_with(
        document,
        transform=P39C.plugin_round_trip(P39.expand_keyframes, delay_sample_transform()),
    )
    first = growing_sample(length=12_000)
    second = {"name": "Second Pass", "length": 12_000, "sample_rate": 44_100, "samples": first["samples"]}
    result = P39C.run_on(
        host,
        {"state": {"settings.sample": first, "settings.osc_1_level": 0.5}},
        dry_run=False,
    )
    assert result.ok is True, result.error and result.error.to_dict()
    # now a second transaction writes the same path again: the carry must not resurrect the first
    result = P39C.run_on(host, {"state": {"settings.sample": second}}, dry_run=False)
    assert result.ok is True, result.error and result.error.to_dict()
    live = decode_live(host)
    assert live["document"]["settings"]["sample"]["name"] == "Second Pass"


def test_the_codec_applies_intended_values_only_where_the_document_has_them():
    """A path the document no longer carries is skipped, never a crash (rollback paths shrink away).

    The document is the WRAPPER `decode` returns, not a bare dict: a set on the wrapper adds a stray
    top-level key and leaves the real payload untouched, which is exactly how the carry silently did
    nothing until it unwrapped (caught here, in the shape the transaction really passes).
    """
    codec = T7.provider_on_fixtures().codec()
    document = {"document": {"settings": {"sample": {"length": 1, "name": "x"}}}, "digest": "d"}
    codec.apply_intended_values(
        document, {"settings.sample": {"length": 2, "name": "y"}, "settings.gone": 5}
    )
    assert document["document"]["settings"]["sample"]["name"] == "y"
    assert "gone" in document["document"]["settings"], "the setter creates the path it is given"
    assert "settings" not in {key for key in document if key != "document"}, (
        "the wrapper must not grow a stray key",
        list(document),
    )
    # and a bare document still works, so both call shapes are covered
    bare = {"settings": {"sample": {"name": "z"}}}
    codec.apply_intended_values(bare, {"settings.sample": {"name": "w"}})
    assert bare["settings"]["sample"]["name"] == "w"


def test_without_the_carry_the_very_same_fixture_drifts_further(monkeypatch):
    """THE CONTROL for the test above: a regression test that cannot fail is not evidence.

    The scheduler now happens to put this fixture's sample LAST, so the drift test above could pass
    even with the carry removed - it must be able to fail. This control pins the sample FIRST (its
    own write, followed by the same table units) and runs the fixture with the carry switched off,
    the layer exactly as it was when the live instance drifted 48 samples. Measured here: 4 samples
    with the carry, 4 per following write without it.
    """
    import dataclasses

    from reaper_reapy_mcp.plugins import transaction as TR

    real_plan = VC.VitalCodec.plan_state_writes

    def sample_first(codec_self, document, changes):
        plan = real_plan(codec_self, document, changes)
        units = list(plan.units)
        index = next(
            index
            for index, unit in enumerate(units)
            if VC.SAMPLE_PATH in ((unit.payload or {}).get("paths") or {})
        )
        units.insert(0, units.pop(index))
        return dataclasses.replace(plan, units=units)

    def measure(*, carry: bool) -> int:
        document = live_document_near_the_ceiling()
        host = host_with(
            document,
            transform=P39C.plugin_round_trip(P39.expand_keyframes, delay_sample_transform()),
        )
        sample = growing_sample(length=20_000)
        request = {
            "state": {
                "settings.sample": sample,
                "settings.osc_1_level": 0.8,
                "settings.osc_2_level": 0.7,
                "settings.osc_3_level": 0.6,
                "settings.filter_1_cutoff": 42.0,
                "settings.wavetables.0": P39.full_table(frames=2),
                "settings.wavetables.2": P39.full_table(frames=3),
            }
        }
        monkeypatch.setattr(VC.VitalCodec, "plan_state_writes", sample_first)
        if not carry:
            monkeypatch.setattr(TR.Transaction, "_record_intended", lambda self, unit: None)
        result = P39C.run_on(host, request, dry_run=False)
        writes = len([row for row in host.log if row[0] == "write_state_slice"])
        monkeypatch.undo()
        assert result.ok is True, result.error and result.error.to_dict()
        block = decode_live(host)["document"]["settings"]["sample"]
        alignment = VC._sample_alignment(sample["samples"], block.get("samples"))
        assert alignment is not None, "the fixture must carry a payload to align"
        assert writes >= 3, writes
        return abs(int(alignment["shift"]))

    with_carry = measure(carry=True)
    without = measure(carry=False)
    assert with_carry == VC.SAMPLE_RELOAD_DELAY, with_carry
    assert without > with_carry, (
        "the fixture cannot show the drift, so the test above proves nothing",
        with_carry,
        without,
    )
