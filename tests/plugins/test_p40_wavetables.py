"""P40: building our OWN wavetables - the sound-design channel (`plugin_wavetable`).

The layer could already deliver a table that came from a preset file. This pins the other half: a
caller DESCRIBES a waveform (a named shape, an additive spectrum, raw samples), the layer builds the
plugin's own table object from it, and the same measured state channel delivers it - with the
growth bound, the balloon, the read-back verification and the rollback unchanged.

Everything here is offline (mock host + fixtures). The live confirmation is the probe
`_p40f_own_table.py`, which writes a morph into a real oscillator and reads its partials back.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

import mock_host as MH  # noqa: E402

from reaper_reapy_mcp.plugins import api as A  # noqa: E402
from reaper_reapy_mcp.plugins import model as M  # noqa: E402
from reaper_reapy_mcp.plugins.errors import InvalidValue, UnsupportedProvider  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import tables as VT  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import wavetables as WT  # noqa: E402

import test_p7_vital as T7  # noqa: E402
import test_p39_balloon as P39  # noqa: E402
import test_p39c_audio_slots as P39C  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


def vital_host(document: dict | None = None, *, expand: bool = True) -> MH.MockHostBridge:
    """A mock host that mirrors the MEASURED plugin behaviour.

    ``expand`` installs the re-serialization the balloon depends on (measured: the plugin writes
    one FULL frame per keyframe whatever payload was written). Without it a balloon opens no room
    and the verification refuses it - which is exactly what the mock is for, but it is not the
    behaviour any of these tests are about.
    """
    doc = document if document is not None else P39.live_document()
    doc.setdefault("synth_version", LIVE_VERSION)
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(doc))])],
        state_write_transform=P39.expand_keyframes if expand else None,
    )
    host.slot(0, 0).version = LIVE_VERSION
    return host


@pytest.fixture
def vital_api(tmp_path, monkeypatch):
    from reaper_reapy_mcp.plugins.providers import registry as PR

    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setitem(PR._FACTORIES, "vital", lambda: T7.provider_on_fixtures())
    return A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})


def live_slot(host: MH.MockHostBridge, oscillator: int = 1) -> dict:
    codec = T7.provider_on_fixtures().codec()
    ref = M.PluginInstanceRef(
        host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
    )
    document = codec.decode(
        host.locate_state_slice(ref, host.fx_facts(0, 0), host.read_state_chunk(ref))
    )["document"]
    return document["settings"]["wavetables"][oscillator - 1]


# ---------------------------------------------------------------------------
# 1. The spec -> frames: shapes, spectra, morphs, and every refusal
# ---------------------------------------------------------------------------


def test_the_named_shapes_are_the_ones_a_synth_starts_from():
    """A naive saw is the plugin's own Init shape (measured h2 = 0.500, h3 = 0.333 = 1/n)."""
    saw = VT.frame_summary(VT.shape_wave("saw"), harmonics=4)
    assert saw["partials"] == [0.6366, 0.3183, 0.2122, 0.1592]  # 2/pi * 1/n
    sine = VT.frame_summary(VT.shape_wave("sine"), harmonics=3)
    assert sine["partials"][0] == 1.0 and sine["partials"][1:] == [0.0, 0.0]
    square = VT.frame_summary(VT.shape_wave("square"), harmonics=5)
    assert square["partials"][1] == 0.0, "a square carries no even partials"
    triangle = VT.shape_wave("triangle")
    assert triangle[0] == pytest.approx(1.0) and max(abs(v) for v in triangle) == 1.0
    pulse = VT.shape_wave("pulse", samples=8, duty=0.25)
    assert pulse.count(1.0) == 2 and pulse.count(-1.0) == 6
    assert set(VT.SHAPES) == {"sine", "triangle", "saw", "square", "pulse"}


def test_a_frame_is_exactly_the_plugin_frame_width():
    """Measured: the plugin memcpys one frame sized `kWaveformSize` - so a short payload is PADDED,
    which would be a different waveform from the one described. The builder resamples instead."""
    short = VT.spec_frames({"samples": [0.0, 1.0, 0.0, -1.0]})[0]
    assert len(short) == VT.FRAME_SAMPLES
    assert short[0] == pytest.approx(0.0) and short[-1] == pytest.approx(-1.0)
    assert len(VT.spec_frames({"shape": "sine"})[0]) == VT.FRAME_SAMPLES
    # values are clamped: the frame reaches the extremes and never leaves [-1, 1]
    clamped = VT.spec_frames({"samples": [-9.0, 9.0]})[0]
    assert min(clamped) == -1.0 and max(clamped) == 1.0
    assert all(-1.0 <= value <= 1.0 for value in VT.shape_wave("saw"))


def test_a_two_item_spec_with_frames_is_a_morph():
    spec = {"shape": ["sine", "square"], "frames": 16}
    described = VT.describe_spec(spec)
    assert described["frames"] == 16
    assert described["positions"] == [int(round(index * 256 / 16)) for index in range(16)]
    frames = VT.spec_frames(spec)
    first = VT.frame_summary(frames[0], harmonics=6)["partials"]
    last = VT.frame_summary(frames[-1], harmonics=6)["partials"]
    assert first[0] == 1.0 and first[1] == 0.0, "the morph starts at a pure sine"
    assert last[1] == 0.0 and last[2] > 0.0, "and ends at a square (odd partials only)"
    middle = VT.frame_summary(frames[8], harmonics=6)["partials"]
    assert 0.0 < middle[2] < last[2], "the frames between are interpolated"


def test_the_spec_refuses_what_it_cannot_build_and_says_what_it_accepts():
    with pytest.raises(InvalidValue) as two_sources:
        VT.spec_frames({"shape": "saw", "harmonics": [[1, 1.0]]})
    assert "EXACTLY ONE source" in str(two_sources.value)
    with pytest.raises(InvalidValue) as unknown_shape:
        VT.spec_frames({"shape": "bogus"})
    assert "bogus" in str(unknown_shape.value)
    with pytest.raises(InvalidValue) as unknown_key:
        VT.spec_frames({"shape": "saw", "wobble": 3})
    assert unknown_key.value.detail.get("unknown") == ["wobble"]
    assert "shape" in unknown_key.value.detail.get("accepted", [])
    with pytest.raises(InvalidValue) as too_many:
        VT.spec_frames({"shape": "saw", "frames": VT.MAX_FRAMES + 1})
    assert str(VT.MAX_FRAMES) in str(too_many.value)
    with pytest.raises(InvalidValue) as nan:
        VT.spec_frames({"samples": [float("nan"), 0.0]})
    assert "NaN" in str(nan.value) or "infinite" in str(nan.value)
    with pytest.raises(InvalidValue) as bad_harmonic:
        VT.spec_frames({"harmonics": [[0, 1.0]]})
    assert "whole number" in str(bad_harmonic.value)
    with pytest.raises(InvalidValue) as bad_duty:
        VT.spec_frames({"shape": "pulse", "duty": 1.5})
    assert "duty" in str(bad_duty.value)


def test_an_additive_spectrum_builds_the_waveform_it_names():
    """`harmonics` is the sound-design primitive: a spectrum is a few numbers.

    The frame is normalised to peak 1.0 (a wavetable frame that leaves [-1, 1] is a clipped frame),
    so what a caller gets is the spectrum's RATIOS - which is what the timbre is.
    """
    wave = VT.harmonics_wave([[1, 1.0], [3, 0.5], [5, 0.25]])
    described = VT.frame_summary(wave, harmonics=6)
    assert max(abs(value) for value in wave) == pytest.approx(1.0, abs=0.01)
    assert described["partials"][1] == pytest.approx(0.0, abs=0.01), "no second partial was asked for"
    assert described["partials"][2] / described["partials"][0] == pytest.approx(0.5, abs=0.02)
    assert described["partials"][4] / described["partials"][0] == pytest.approx(0.25, abs=0.02)
    # one spectrum per frame is a morph written as spectra
    spec = {"harmonics": [[[1, 1.0]], [[1, 1.0], [2, 0.5]]]}
    assert VT.describe_spec(spec)["frames"] == 2
    frames = VT.spec_frames(spec)
    assert VT.frame_summary(frames[1], harmonics=3)["partials"][1] > 0.1


def test_the_built_table_is_the_plugin_shape_with_the_live_version():
    table = VT.build_table({"shape": "saw", "name": "My Saw"}, live_version=LIVE_VERSION)
    assert sorted(table) == ["author", "full_normalize", "groups", "name", "remove_all_dc", "version"]
    assert table["version"] == LIVE_VERSION, "the plugin restamps this, so it must match"
    component = table["groups"][0]["components"][0]
    assert component["type"] == "Wave Source"
    rows = component["keyframes"]
    assert len(rows) == 1 and rows[0]["position"] == 0
    assert len(rows[0]["wave_data"]) == WT.full_frame_chars()
    assert WT.carries_waves(table) is True
    assert WT.describe(table, oscillator=1)["source_types"] == ["Wave Source"]


# ---------------------------------------------------------------------------
# 2. The tool: preview host-free, read the live slot, write through the channel
# ---------------------------------------------------------------------------


def test_preview_needs_no_host_at_all(vital_api):
    data = A.plugin_wavetable(
        config=vital_api,
        op="preview",
        table={"shape": ["sine", "saw"], "frames": 8, "name": "Morph"},
    )["data"]
    assert data["described"]["frames"] == 8
    assert data["frame_indices"] == [0, 7], "the two ends of the morph are what a caller needs"
    assert data["frames"][0]["partials"][0] == 1.0
    assert data["frames"][-1]["partials"][1] > 0.0
    assert data["shapes"] == list(VT.SHAPES), "the vocabulary travels with the answer"
    assert data["described"]["payload_bytes"] > 0


def test_read_reports_what_the_slot_holds(vital_api, monkeypatch):
    monkeypatch.setattr(A, "_BRIDGE", vital_host())
    data = A.plugin_wavetable(config=vital_api, instance=dict(INSTANCE), op="read", oscillator=1)["data"]
    assert data["oscillator"] == 1
    assert data["carries_waves"] is True
    assert data["table_id"], "a table needs its content identity"
    assert data["frame_summaries"], "the frames of the live table are summarised"


def test_write_delivers_the_table_and_it_reads_back(vital_api, monkeypatch):
    host = vital_host()
    monkeypatch.setattr(A, "_BRIDGE", host)
    spec = {"shape": ["sine", "square"], "frames": 8, "name": "MCP Morph"}

    dry = A.plugin_wavetable(
        config=vital_api, instance=dict(INSTANCE), op="write", oscillator=2, table=spec, dry_run=True
    )
    assert dry["ok"] is True and dry["data"]["would_write"] is not None
    assert dry["data"]["path"] == "settings.wavetables.1"
    assert [row for row in host.log if row[0] == "write_state_slice"] == [], "a dry run writes nothing"

    real = A.plugin_wavetable(
        config=vital_api, instance=dict(INSTANCE), op="write", oscillator=2, table=spec, dry_run=False
    )
    data = real["data"]
    assert real["ok"] is True, real.get("error")
    assert data["delivered"] is True, data.get("gaps")
    writes = [row for row in host.log if row[0] == "write_state_slice"]
    assert len(writes) >= 1, host.log

    slot = live_slot(host, oscillator=2)
    assert len(WT.keyframes(slot)) == 8, "all eight frames landed"
    assert WT.describe(slot, oscillator=2)["source_types"] == ["Wave Source"]
    first = VT.frame_summary(WT.decode_frame(slot, 0), harmonics=4)["partials"]
    last = VT.frame_summary(WT.decode_frame(slot, 7), harmonics=4)["partials"]
    assert first[0] == pytest.approx(1.0, abs=0.02), "frame 0 is the sine the spec named"
    assert last[1] == pytest.approx(0.0, abs=0.02) and last[2] > 0.05, "frame 7 is the square"
    # and the tool can read its own work back
    read = A.plugin_wavetable(
        config=vital_api, instance=dict(INSTANCE), op="read", oscillator=2, frames=8
    )["data"]
    assert read["n_frames"] == 8 and len(read["frame_summaries"]) == 8
    assert read["frame_indices"] == list(range(8))


def test_a_one_frame_table_replaces_a_big_one_as_a_shrink(vital_api, monkeypatch):
    """A small table over a big one needs no balloon - and must still be verified."""
    big = P39C.heavy_table()
    document = P39.live_document(big, big, big)
    document["synth_version"] = LIVE_VERSION
    host = vital_host(document)
    monkeypatch.setattr(A, "_BRIDGE", host)

    real = A.plugin_wavetable(
        config=vital_api,
        instance=dict(INSTANCE),
        op="write",
        oscillator=1,
        table={"shape": "triangle", "name": "Small"},
        dry_run=False,
    )
    assert real["ok"] is True, real.get("error")
    slot = live_slot(host, oscillator=1)
    assert len(WT.keyframes(slot)) == 1
    partials = VT.frame_summary(WT.decode_frame(slot, 0), harmonics=6)["partials"]
    assert partials[1] == pytest.approx(0.0, abs=0.02) and partials[3] == pytest.approx(0.0, abs=0.02), (
        "a triangle carries odd partials only"
    )
    assert partials[0] > 0.5


def test_a_provider_without_the_vocabulary_is_refused_by_name(tmp_path, monkeypatch):
    """The universal boundary: the table vocabulary belongs to the provider."""
    from reaper_reapy_mcp.plugins.providers import registry as PR

    class NoTables(T7.provider_on_fixtures().__class__):  # type: ignore[misc]
        def wavetables(self):  # noqa: D102 - deliberately absent capability
            return None

    monkeypatch.setitem(PR._FACTORIES, "vital", lambda: NoTables())
    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    monkeypatch.setattr(A, "_BRIDGE", vital_host())
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})
    with pytest.raises(UnsupportedProvider) as refusal:
        A.plugin_wavetable(config=config, instance=dict(INSTANCE), op="read", oscillator=1)
    assert "no wavetable capability" in str(refusal.value)


def test_write_needs_a_spec_and_names_the_vocabulary(vital_api, monkeypatch):
    monkeypatch.setattr(A, "_BRIDGE", vital_host())
    with pytest.raises(InvalidValue) as refusal:
        A.plugin_wavetable(config=vital_api, instance=dict(INSTANCE), op="write", oscillator=1)
    assert "shape" in str(refusal.value) and "harmonics" in str(refusal.value)
