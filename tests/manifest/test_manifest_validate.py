"""Offline checks of audiocpp.manifest v1 load/validate/clamp (reaper side).

Fixtures are built in-test (a 0.1 s silent WAV via the stdlib wave module and a
minimal MIDI via mido) so no binaries are committed. Nothing here imports reapy
or connects to a host.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from reaper_reapy_mcp.manifest import manifest
from reaper_reapy_mcp.manifest.manifest import ManifestError, clamp_regions


def _write_wav(path: Path, seconds: float = 0.1) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 2 * int(48000 * seconds))


def _write_midi(path: Path) -> None:
    from mido import Message, MidiFile, MidiTrack

    midi = MidiFile()
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(Message("note_on", note=60, velocity=64, time=0))
    track.append(Message("note_off", note=60, velocity=0, time=480))
    midi.save(str(path))


def _bundle_doc(**overrides) -> dict:
    doc = {
        "schema": "audiocpp.manifest",
        "version": 1,
        "created_utc": "2026-10-12T00:00:00Z",
        "title": "test_bundle",
        "bpm": 172.0,
        "meter": [4, 4],
        "key": "D#m",
        "duration_s": 270.0,
        "audio": {"file": "song.wav"},
        "midi": {"vocal": "vocal.mid", "ins": "ins.mid"},
        "regions": [
            {"name": "intro", "start_s": 0.0, "end_s": 11.26, "source": "plan"},
            {"name": "verse", "start_s": 11.26, "end_s": 50.25, "source": "plan"},
        ],
        "stems": {},
        "provenance": {"producer": "mcp-audiocpp"},
        "warnings": ["plan total 281.86 s differs from audio duration 269.96 s by 4.4%"],
        "notes": "",
    }
    doc.update(overrides)
    return doc


def _write_bundle(base: Path, *, audio: bool = True, midi: bool = True, **doc_overrides) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    if audio:
        _write_wav(base / "song.wav")
    if midi:
        _write_midi(base / "vocal.mid")
        _write_midi(base / "ins.mid")
    (base / "manifest.json").write_text(
        json.dumps(_bundle_doc(**doc_overrides), ensure_ascii=False), encoding="utf-8"
    )
    return base


@pytest.fixture()
def pointer(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "pointer" / "_latest_manifest.txt"
    monkeypatch.setenv("AUDIOCPP_MANIFEST_POINTER", str(path))
    return path


def test_happy_load_resolves_absolute_paths(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "good")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(str(bundle), encoding="utf-8")

    resolved = manifest.resolve_bundle_dir("latest")
    assert resolved == bundle

    info = manifest.load_manifest(resolved)
    assert info["bpm"] == 172.0
    assert info["meter"] == [4, 4]
    assert info["duration_s"] == 270.0
    assert info["title"] == "test_bundle"
    assert [r["name"] for r in info["regions"]] == ["intro", "verse"]
    assert Path(info["audio_path"]).name == "song.wav"
    assert Path(info["vocal_midi_path"]).name == "vocal.mid"
    assert Path(info["ins_midi_path"]).name == "ins.mid"
    assert info["warnings"]  # carried from the manifest


def test_unknown_fields_are_tolerated(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "forward", future_field={"anything": True})
    info = manifest.load_manifest(bundle)
    assert info["bpm"] == 172.0


def test_missing_pointer_refuses(tmp_path, pointer):
    with pytest.raises(ManifestError) as excinfo:
        manifest.resolve_bundle_dir("latest")
    assert "pointer" in str(excinfo.value)
    assert excinfo.value.hints


def test_explicit_bundle_dir_wins(tmp_path, pointer):
    assert manifest.resolve_bundle_dir(str(tmp_path / "somewhere")) == tmp_path / "somewhere"


def test_foreign_schema_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "schema", schema="something.else")
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "schema" in str(excinfo.value)


def test_version_two_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "v2", version=2)
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "version" in str(excinfo.value)


def test_bpm_zero_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "bpm", bpm=0)
    with pytest.raises(ManifestError):
        manifest.load_manifest(bundle)


def test_missing_audio_file_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "noaudio", audio=False)
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "audio" in str(excinfo.value)


def test_missing_midi_file_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "nomidi", midi=False)
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "MIDI" in str(excinfo.value)


def test_empty_regions_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "noregions", regions=[])
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "regions" in str(excinfo.value)


def test_region_end_not_after_start_refuses(tmp_path, pointer):
    regions = [{"name": "intro", "start_s": 10.0, "end_s": 10.0, "source": "plan"}]
    bundle = _write_bundle(tmp_path / "badregion", regions=regions)
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "greater" in str(excinfo.value)


def test_unknown_region_source_refuses(tmp_path, pointer):
    regions = [{"name": "intro", "start_s": 0.0, "end_s": 5.0, "source": "magic"}]
    bundle = _write_bundle(tmp_path / "badsource", regions=regions)
    with pytest.raises(ManifestError) as excinfo:
        manifest.load_manifest(bundle)
    assert "source" in str(excinfo.value)


def test_bad_meter_refuses(tmp_path, pointer):
    bundle = _write_bundle(tmp_path / "badmeter", meter=[4])
    with pytest.raises(ManifestError):
        manifest.load_manifest(bundle)


def test_clamp_regions_keeps_clamps_and_skips():
    # A region still sounding when the audio ends is CLAMPED; one that starts at/
    # past the duration has no audible range left and is SKIPPED, not clamped.
    regions = [
        {"name": "a", "start_s": 0.0, "end_s": 300.0, "source": "plan"},
        {"name": "b", "start_s": 100.0, "end_s": 200.0, "source": "plan"},
        {"name": "c", "start_s": 260.0, "end_s": 400.0, "source": "mixed"},
        {"name": "d", "start_s": 280.0, "end_s": 400.0, "source": "plan"},
    ]
    kept, clamps, skipped = clamp_regions(regions, 270.0)
    assert [r["name"] for r in kept] == ["a", "b", "c"]
    assert kept[0]["end_s"] == 270.0
    assert kept[1]["end_s"] == 200.0
    assert kept[2]["end_s"] == 270.0
    assert clamps == [
        {"name": "a", "end_s": 300.0, "applied_end_s": 270.0},
        {"name": "c", "end_s": 400.0, "applied_end_s": 270.0},
    ]
    assert [s["name"] for s in skipped] == ["d"]


def test_clamp_regions_skips_region_starting_past_duration():
    regions = [{"name": "ghost", "start_s": 400.0, "end_s": 500.0, "source": "plan"}]
    kept, clamps, skipped = clamp_regions(regions, 270.0)
    assert kept == []
    assert clamps == []
    assert skipped and skipped[0]["name"] == "ghost"


def test_ai_tracks_constant():
    assert manifest.AI_TRACKS == ("AI_MASTER", "AI_VOCAL", "AI_INSTR", "MIDI_VOCAL", "MIDI_INS")
