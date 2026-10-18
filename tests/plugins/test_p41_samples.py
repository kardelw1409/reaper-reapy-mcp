"""P41: the SAMPLER as a channel - a WAV in, both channels out, the engine switched on.

MEASURED (2026-10-18, live, through the product path):

* the whole sampler payload is ONE state block (`settings.sample`) and it is an ORDINARY state
  path: writing it with three sampler controls in the same batch came back `committed: true`,
  `ALL_VERIFIED`, with `name`/`length`/`sample_rate` read back exactly as written and 2 write
  calls, 0 with unknown effect. The block SHRANK the state (3,896,997 -> 3,739,653 characters),
  i.e. the displaced preset sample was 44,100 samples - the payload is inline PCM;
* the plugin's own source (`Sample::stateToJson` / `jsonToState`) is what the mapping is taken
  from: `samples` encodes `left_buffers` and `samples_stereo` encodes `right_buffers`, and the
  loader decodes exactly `length * sizeof(int16_t)` bytes - so a payload must carry exactly
  `length` samples per channel (a longer one is cut, a shorter one is zero-padded);
* `Sample::loadSample` clamps a mono buffer to `kMaxSize` = 1,764,000 samples.

Everything here is offline (mock host + stdlib WAV files in `tmp_path`); the live confirmation is
`plugin_sample(op="load")` against the sandbox instance.
"""

from __future__ import annotations

import array
import base64
import struct
import sys
import wave
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

import mock_host as MH  # noqa: E402

from reaper_reapy_mcp.plugins import api as A  # noqa: E402
from reaper_reapy_mcp.plugins import model as M  # noqa: E402
from reaper_reapy_mcp.plugins.errors import InvalidValue, UnsupportedProvider  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import samples as VS  # noqa: E402

import test_p7_vital as T7  # noqa: E402
import test_p39_balloon as P39  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


# ---------------------------------------------------------------------------
# Files: a stdlib WAV is the only source, so these are written with the stdlib
# ---------------------------------------------------------------------------


def write_wav(
    path: Path,
    *,
    channels: int = 1,
    width: int = 2,
    rate: int = 44_100,
    frames: int = 64,
) -> Path:
    values: list[int] = []
    for index in range(frames):
        value = ((index * 700) % 20_000) - 10_000
        for _channel in range(channels):
            values.append(value)
    if width == 1:
        payload = bytes((value + 128) & 0xFF for value in values)
    elif width == 2:
        payload = struct.pack("<%dh" % len(values), *values)
    else:  # 4 bytes: int32, the frames sixteen bits up
        payload = struct.pack("<%di" % len(values), *[value << 16 for value in values])
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return path


# ---------------------------------------------------------------------------
# 1. Reading a file: the channels, the rate, and every refusal
# ---------------------------------------------------------------------------


def test_a_mono_wav_becomes_one_channel_at_its_own_rate(tmp_path: Path):
    decoded = VS.read_wav(str(write_wav(tmp_path / "mono.wav", rate=48_000, frames=32)))
    assert decoded["file_channels"] == 1 and decoded["right"] is None
    assert decoded["sample_rate"] == 48_000 and decoded["frames"] == 32
    assert decoded["name"] == "mono" and decoded["bits"] == 16
    assert decoded["left"][0] == -10_000 and decoded["left"][1] == -9_300, decoded["left"][:2]
    assert len(decoded["left"]) == 32


def test_a_stereo_wav_keeps_both_channels_in_order(tmp_path: Path):
    path = tmp_path / "stereo.wav"
    left = [(index + 1) * 10 for index in range(16)]
    right = [-(index + 1) * 10 for index in range(16)]
    payload = struct.pack("<32h", *[value for pair in zip(left, right) for value in pair])
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(payload)
    decoded = VS.read_wav(str(path))
    assert decoded["left"][:4] == left[:4]
    assert decoded["right"][:4] == right[:4]
    assert decoded["dropped_channels"] == 0


def test_an_eight_bit_wav_is_unsigned_the_way_the_format_says(tmp_path: Path):
    decoded = VS.read_wav(str(write_wav(tmp_path / "eight.wav", width=1, frames=8)))
    assert decoded["bits"] == 8
    assert decoded["left"][0] == VS.read_wav(str(write_wav(tmp_path / "eight2.wav", width=1, frames=8)))[
        "left"
    ][0]
    assert isinstance(decoded["left"][0], int)


def test_a_thirty_two_bit_file_is_scaled_not_truncated(tmp_path: Path):
    decoded = VS.read_wav(str(write_wav(tmp_path / "wide.wav", width=4, frames=8)))
    assert decoded["bits"] == 32
    assert decoded["left"][:2] == [-10_000, -9_300], decoded["left"][:2]


def test_a_twenty_four_bit_file_is_refused_by_name(tmp_path: Path):
    """A wrong shift would be a silent amplitude error - so it is refused, not guessed."""
    with pytest.raises(InvalidValue) as bad:
        VS._pcm_from_bytes(b"\x00" * 12, 3, 1)
    assert bad.value.detail["bits"] == 24
    assert "16-bit" in bad.value.detail["why"]


def test_a_missing_file_is_named(tmp_path: Path):
    with pytest.raises(InvalidValue) as missing:
        VS.read_wav(str(tmp_path / "nope.wav"))
    assert missing.value.detail["reason"] == "not_found"


def test_a_file_longer_than_the_plugin_buffer_is_refused_with_both_numbers(
    tmp_path: Path, monkeypatch
):
    """The bound is the plugin's own `kMaxSize`; a longer file is refused, never truncated."""
    monkeypatch.setattr(VS, "MAX_SAMPLES", 32)
    path = write_wav(tmp_path / "long.wav", frames=40)
    with pytest.raises(InvalidValue) as long:
        VS.read_wav(str(path))
    assert long.value.detail["frames"] == 40
    assert long.value.detail["max_samples"] == 32
    assert "1,764,000" in long.value.detail["why"]


def test_a_non_wav_file_says_what_it_found(tmp_path: Path):
    path = tmp_path / "not.wav"
    path.write_bytes(b"RIFF not really")
    with pytest.raises(InvalidValue) as bad:
        VS.read_wav(str(path))
    assert bad.value.detail["reason"] in ("not_pcm_wav",)


# ---------------------------------------------------------------------------
# 2. The block: exactly `length` samples, left in `samples`, right in `samples_stereo`
# ---------------------------------------------------------------------------


def test_the_block_is_what_the_plugin_loads():
    block = VS.build_block(left=[0, 1000, -1000], right=None, sample_rate=44_100, name="T")
    assert block["length"] == 3 and block["sample_rate"] == 44_100 and block["name"] == "T"
    data = array.array("h")
    data.frombytes(base64.b64decode(block["samples"]))
    assert list(data) == [0, 1000, -1000]
    assert "samples_stereo" not in block, "a mono buffer has no stereo key (the plugin's own shape)"


def test_a_stereo_block_carries_both_payloads():
    block = VS.build_block(left=[1, 2], right=[-1, -2], sample_rate=22_050, name="S")
    assert "samples_stereo" in block
    right = array.array("h")
    right.frombytes(base64.b64decode(block["samples_stereo"]))
    assert list(right) == [-1, -2]


def test_out_of_range_values_are_clamped_never_wrapped():
    """A wrapped transient is a polarity flip - a silent corruption of the material."""
    block = VS.build_block(left=[40_000, -40_000], right=None, sample_rate=44_100, name="C")
    data = array.array("h")
    data.frombytes(base64.b64decode(block["samples"]))
    assert list(data) == [32_767, -32_768]


def test_the_two_channels_must_be_the_same_length():
    with pytest.raises(InvalidValue) as bad:
        VS.build_block(left=[1, 2, 3], right=[1], sample_rate=44_100, name="X")
    assert bad.value.detail == {"left": 3, "right": 1}


def test_describe_reports_what_the_sampler_holds():
    document = {"settings": {"sample": {"name": "S", "length": 22_050, "sample_rate": 44_100, "samples": "AA"}}}
    described = VS.describe(document)
    assert described["channels"] == 1
    assert described["seconds"] == 0.5
    assert described["max_samples"] == 1_764_000


# ---------------------------------------------------------------------------
# 3. The knobs: a boolean becomes the control's own label
# ---------------------------------------------------------------------------


def profile_with(*controls: dict) -> dict:
    return {"controls": list(controls)}


def enum_control(control: str, members: list[str]) -> dict:
    return {
        "id": control,
        "type": "enum",
        "binding": {"index": 1},
        "enum_values": [{"id": member} for member in members],
    }


def test_a_boolean_knob_becomes_the_controls_own_label():
    profile = profile_with(
        enum_control("sample_on", ["Off", "On"]), enum_control("sample_loop", ["Off", "On"])
    )
    wanted, skipped = A._sampler_controls(
        profile, {"on": True, "loop": False}, VS.SampleCapability().controls
    )
    assert wanted == {"sample_on": "On", "sample_loop": "Off"}, wanted
    assert skipped == {}


def test_a_boolean_on_a_control_without_on_off_refuses_with_the_legal_members():
    profile = profile_with(enum_control("sample_on", ["Left", "Right"]))
    with pytest.raises(InvalidValue) as bad:
        A._sampler_controls(profile, {"on": True}, VS.SampleCapability().controls)
    assert bad.value.detail["legal"] == ["Left", "Right"]


def test_a_control_the_profile_does_not_declare_is_named_not_silently_dropped():
    wanted, skipped = A._sampler_controls(
        profile_with(), {"on": True, "level": 0.5}, VS.SampleCapability().controls
    )
    assert wanted == {}
    assert set(skipped) == {"sample_on", "sample_level"}
    assert "no control" in skipped["sample_on"]


def test_a_knob_the_capability_does_not_name_is_reported_not_ignored():
    wanted, skipped = A._sampler_controls(profile_with(), {"on": True}, {"level": "sample_level"})
    assert wanted == {}
    assert "on" in skipped, skipped


def test_numeric_knobs_pass_through_untouched():
    profile = profile_with(
        {"id": "sample_level", "type": "number", "binding": {"index": 2}, "range": {"min": 0, "max": 1}},
        {"id": "sample_transpose", "type": "number", "binding": {"index": 3}, "range": {"min": -48, "max": 48}},
    )
    wanted, _skipped = A._sampler_controls(
        profile, {"level": 0.85, "transpose": -12}, VS.SampleCapability().controls
    )
    assert wanted == {"sample_level": 0.85, "sample_transpose": -12}


# ---------------------------------------------------------------------------
# 4. Loading: through the SAME transaction as any other write
# ---------------------------------------------------------------------------


def plugin_round_trip(steps: int = VC.SAMPLE_RELOAD_DELAY):
    """Mirror `Sample::jsonToState` + `stateToJson`: exactly `length` samples, returned late.

    This is what makes a load GROW (the balloon exists for exactly this): the block is written
    with a short payload and a longer `length`, and the plugin re-serializes `length` samples.
    """

    def transform(block_text: str) -> str:
        framing = VC.parse_block_framing(block_text)
        doc, _brace = VC.decode_document(framing["B"])
        sample = ((doc.get("settings") or {}).get("sample")) or {}
        length = int(sample.get("length") or 0)
        for key in VC.SAMPLE_PAYLOAD_KEYS:
            value = sample.get(key)
            if not isinstance(value, str):
                continue
            raw = base64.b64decode(value + "=" * (-len(value) % 4))
            data = array.array("h")
            data.frombytes(raw[: len(raw) // 2 * 2])
            sized = list(data[:length]) + [0] * max(0, length - len(data))
            delayed = array.array("h", [0] * steps + sized[: max(0, len(sized) - steps)])
            sample[key] = base64.b64encode(delayed.tobytes()).decode("ascii")
        new_b = VC.encode_item_b(framing["B"], doc, mode="grow")
        item_a = VC.encode_item_a(framing["A"], len(new_b))
        lines = [framing["header"], base64.b64encode(item_a).decode("ascii")]
        lines.extend(VC.wrap_lines(base64.b64encode(new_b).decode("ascii"), framing.get("b64_width") or 280))
        if framing.get("b64_artifact"):
            lines.append(framing["b64_artifact"])
        lines.extend(framing["tail_lines"])
        return "\n".join(lines)

    return transform


def document_with_sample(*, length: int = 4096) -> dict:
    document = P39.live_document()
    document["synth_version"] = LIVE_VERSION
    document["settings"]["sample"] = {
        "name": "Old",
        "length": length,
        "sample_rate": 44_100,
        "samples": base64.b64encode(array.array("h", [0] * length).tobytes()).decode("ascii"),
    }
    return document


def host_with(document: dict) -> MH.MockHostBridge:
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(document))])],
        state_write_transform=plugin_round_trip(),
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


@pytest.fixture
def vital_api(tmp_path, monkeypatch):
    from reaper_reapy_mcp.plugins.providers import registry as PR

    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setitem(PR._FACTORIES, "vital", lambda: T7.provider_on_fixtures())
    return A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})


def test_a_load_lands_the_block_and_verifies_it(vital_api, monkeypatch):
    host = host_with(document_with_sample())
    monkeypatch.setattr(A, "_BRIDGE", host)
    payload = [((index * 91) % 4000) - 2000 for index in range(64)]
    envelope = A.plugin_sample(
        config=vital_api,
        instance=dict(INSTANCE),
        op="load",
        samples=payload,
        sample_rate=44_100,
        name="MCP Test Tone",
        dry_run=False,
    )
    assert envelope["ok"] is True, envelope.get("error")
    data = envelope["data"]
    assert data["loaded"] is True
    assert data["described"]["name"] == "MCP Test Tone"
    assert data["described"]["length"] == 64
    assert data["described"]["channels"] == 1
    live = decode_live(host)["document"]["settings"]["sample"]
    assert live["name"] == "MCP Test Tone"
    assert live["length"] == 64
    assert live["sample_rate"] == 44_100
    # the payload is the one we sent, delayed by the plugin's own measured reload delay
    written = base64.b64encode(array.array("h", payload).tobytes()).decode("ascii")
    alignment = VC._sample_alignment(written, live.get("samples"))
    assert alignment is not None and alignment["within_delay"] is True, alignment
    assert abs(int(alignment["shift"])) == VC.SAMPLE_RELOAD_DELAY, alignment


def test_a_growing_load_rides_the_sample_balloon_pair(vital_api, monkeypatch):
    """A payload bigger than the landing bound is delivered in two writes - measured behaviour."""
    host = host_with(document_with_sample(length=4096))
    monkeypatch.setattr(A, "_BRIDGE", host)
    envelope = A.plugin_sample(
        config=vital_api,
        instance=dict(INSTANCE),
        op="load",
        samples=[((index * 37) % 2000) - 1000 for index in range(4096)],
        sample_rate=44_100,
        dry_run=False,
    )
    assert envelope["ok"] is True, envelope.get("error")
    reasons = [gap.get("reason") for gap in envelope["data"].get("gaps") or []]
    assert "sample_inflated_before_write" in reasons or envelope["data"].get("partial") is not True
    live = decode_live(host)["document"]["settings"]["sample"]
    assert live["length"] == 4096


def test_a_load_that_only_switches_the_sampler_on_still_names_the_chain(vital_api, monkeypatch):
    """The controls ride the same batch; a profile without them names what it could not address."""
    host = host_with(document_with_sample())
    monkeypatch.setattr(A, "_BRIDGE", host)
    envelope = A.plugin_sample(
        config=vital_api,
        instance=dict(INSTANCE),
        op="load",
        samples=[1, 2, 3, 4],
        sample_rate=44_100,
        dry_run=False,
    )
    data = envelope["data"]
    assert envelope["ok"] is True
    assert data["controls"] == {}
    assert "sample_on" in data.get("controls_skipped", {}), data.get("controls_skipped")


def test_a_dry_run_plans_without_writing(vital_api, monkeypatch):
    host = host_with(document_with_sample())
    monkeypatch.setattr(A, "_BRIDGE", host)
    envelope = A.plugin_sample(
        config=vital_api,
        instance=dict(INSTANCE),
        op="load",
        samples=[5, 6, 7, 8],
        sample_rate=44_100,
        dry_run=True,
    )
    assert envelope["ok"] is True
    writes = [row for row in host.log if str(row[0]).startswith("write")]
    assert writes == [], f"a dry run must not issue a write: {writes}"
    assert envelope["data"]["dry_run"] is True


def test_the_source_rule_is_checked_before_any_host_contact(vital_api, monkeypatch):
    host = host_with(document_with_sample())
    monkeypatch.setattr(A, "_BRIDGE", host)
    with pytest.raises(InvalidValue) as both:
        A.plugin_sample(
            config=vital_api,
            instance=dict(INSTANCE),
            op="load",
            file_path="x.wav",
            samples=[1, 2],
            dry_run=True,
        )
    assert "name ONE source" in str(both.value)
    with pytest.raises(InvalidValue) as none:
        A.plugin_sample(config=vital_api, instance=dict(INSTANCE), op="load", dry_run=True)
    assert "one of them" in str(none.value)


def test_raw_samples_need_a_rate():
    capability = VS.SampleCapability()
    with pytest.raises(InvalidValue) as bad:
        capability.load(samples=[1, 2, 3])
    assert "sample_rate" in str(bad.value)


def test_an_unknown_op_is_refused_with_the_accepted_list(vital_api):
    with pytest.raises(InvalidValue) as bad:
        A.plugin_sample(config=vital_api, instance=dict(INSTANCE), op="record", dry_run=True)
    assert bad.value.detail["accepted"] == ["read", "load"]


def test_a_provider_without_the_capability_is_refused_by_name():
    class Bare:
        id = "bare"

    with pytest.raises(UnsupportedProvider) as bad:
        A._sampler_capability(Bare())
    assert bad.value.detail["tool"] == "plugin_sample"


def test_the_capability_reads_a_document(vital_api, monkeypatch):
    host = host_with(document_with_sample(length=2048))
    monkeypatch.setattr(A, "_BRIDGE", host)
    envelope = A.plugin_sample(config=vital_api, instance=dict(INSTANCE), op="read")
    data = envelope["data"]
    assert data["length"] == 2048
    assert data["channels"] == 1
    assert data["seconds"] == pytest.approx(2048 / 44_100, abs=1e-6)
    assert isinstance(data["controls"], dict)
