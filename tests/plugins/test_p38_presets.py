"""P38: the preset library format, the portable resolver and the catalog (offline).

Nothing here touches a real preset library or a host: every library is built in
`tmp_path` from synthetic documents, so the tests are hermetic and the user's own
corpus never enters the repository (P32 policy, and the presets' own license).
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

from reaper_reapy_mcp.plugins.errors import InvalidValue  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import catalog as CAT  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import preset as PRESET  # noqa: E402


def pcm(values: list[int]) -> str:
    return base64.b64encode(struct.pack("<%dh" % len(values), *values)).decode("ascii")


def preset_document(
    *,
    name: str = "Test",
    author: str = "Unit",
    style: str = "Bass",
    version: str = "1.5.5",
    controls: dict | None = None,
    sample_len: int = 64,
    sample_fill: int = 9,
    modulations: list | None = None,
) -> dict:
    settings = {
        "osc_1_level": 0.5,
        "filter_1_cutoff": 60.0,
        "beats_per_minute": 2.0,
        "sample": {
            "name": "S",
            "length": sample_len,
            "sample_rate": 44100,
            "samples": pcm([sample_fill + index % 5 for index in range(sample_len)]),
        },
        "modulations": modulations
        if modulations is not None
        else [{"source": "lfo_1", "destination": "filter_1_cutoff"}],
        "lfos": [{"num_points": 3, "points": [0.0, 0.0, 1.0, 0.5], "powers": [0.0, 0.0]}],
        "custom_warps": [{"name": "A", "num_points": 3, "points": [0.0, 1.0, 0.5], "powers": [0.0, 0.0]}],
        "random_values": [{"seed": 1}],
        "wavetables": [],
    }
    settings.update(controls or {})
    mods = settings["modulations"] if isinstance(settings["modulations"], list) else []
    for index in range(len(mods)):
        settings.setdefault(f"modulation_{index + 1}_amount", -0.5)
        settings.setdefault(f"modulation_{index + 1}_bipolar", 0.0)
    return {
        "synth_version": version,
        "preset_name": name,
        "author": author,
        "comments": "",
        "preset_style": style,
        "macro1": "MACRO 1",
        "macro2": "MACRO 2",
        "macro3": "MACRO 3",
        "macro4": "MACRO 4",
        "settings": settings,
    }


def write_preset(root: Path, relative: str, document: dict) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Portable path resolution
# ---------------------------------------------------------------------------


def test_env_override_wins(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(PRESET.DATA_DIR_ENV, str(tmp_path / "lib"))
    assert PRESET.data_dir() == (tmp_path / "lib").resolve()


def test_empty_override_is_refused(monkeypatch):
    monkeypatch.setenv(PRESET.DATA_DIR_ENV, "  ")
    with pytest.raises(InvalidValue):
        PRESET.data_dir()


def test_config_key_beats_the_platform_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(PRESET.DATA_DIR_ENV, raising=False)
    configured = tmp_path / "elsewhere"
    config = tmp_path / "Vital.config"
    config.write_text(json.dumps({"data_directory": str(configured)}), encoding="utf-8")
    monkeypatch.setattr(PRESET, "config_file_candidates", lambda env=None: [config])
    assert PRESET.data_dir() == configured.resolve()


def test_platform_default_is_the_last_resort(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(PRESET.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(PRESET, "config_file_candidates", lambda env=None: [tmp_path / "absent.config"])
    monkeypatch.setattr(
        PRESET,
        "platform_data_defaults",
        lambda env=None: [tmp_path / "no-such", tmp_path / "fallback"],
    )
    # neither candidate exists: the first is the declared default (the caller may
    # create it), not a silent refusal
    assert PRESET.data_dir() == tmp_path / "no-such"
    (tmp_path / "fallback").mkdir()
    assert PRESET.data_dir() == tmp_path / "fallback"


def test_safe_join_refuses_escapes_and_wrong_files(tmp_path: Path):
    write_preset(tmp_path, "User/Presets/ok.vital", preset_document())
    (tmp_path / "plain.txt").write_text("x", encoding="utf-8")
    assert PRESET.safe_join(tmp_path, "User/Presets/ok.vital").name == "ok.vital"
    for bad in ("../ok.vital", "/etc/passwd", "C:/windows/x.vital", "User/Presets/none.vital", "plain.txt"):
        with pytest.raises(InvalidValue):
            PRESET.safe_join(tmp_path, bad)


# ---------------------------------------------------------------------------
# Metadata + change set
# ---------------------------------------------------------------------------


def test_metadata_is_read_from_head_and_tail(tmp_path: Path):
    path = write_preset(tmp_path, "User/Presets/meta.vital", preset_document(name="Meta", author="Au", style="Pad"))
    meta = PRESET.read_preset_metadata(path)
    assert meta["name"] == "meta"
    assert meta["embedded_name"] == "Meta"
    assert meta["author"] == "Au"
    assert meta["style"] == "Pad"
    assert meta["synth_version"] == "1.5.5"


def test_change_set_emits_only_differences_and_names_gaps(tmp_path: Path):
    file_doc = preset_document(
        name="New name",
        controls={"osc_1_level": 0.9, "beats_per_minute": 2.5},
    )
    live = preset_document(name="Old", controls={"osc_1_level": 0.5, "beats_per_minute": 2.0})
    outcome = PRESET.change_set(live, file_doc, data_dir_path=tmp_path, preset_id="User/Presets/x.vital")
    paths = outcome["paths"]
    assert paths["settings.osc_1_level"] == 0.9
    assert "settings.filter_1_cutoff" not in paths, "an unchanged control must not be written"
    assert paths["preset_name"] == "New name"
    assert "author" not in paths, "an unchanged label must not be written"
    reasons = [gap["reason"] for gap in outcome["gaps"]]
    assert "host_owned_parameter" in reasons
    assert "settings.beats_per_minute" not in paths


def test_change_set_maps_modulations_onto_route_operations():
    """A pair change is a ROUTE OPERATION, not a state-path diff (P38).

    Measured reason: the transaction's `route_into_off_filter` rule cannot decide a
    state-path `source` write against a slot with an empty live destination - the
    route operation carries the pair explicitly, and a create may name the FILE's
    own slot so the `modulation_N_*` amounts keep pairing with their routing.
    """
    file_doc = preset_document(modulations=[{"source": "lfo_2", "destination": "osc_1_tune"}])
    live = preset_document(modulations=[{"source": "", "destination": ""}])
    outcome = PRESET.change_set(live, file_doc)
    assert outcome["routes_create"] == [
        {
            "slot": 1,
            "source": "lfo_2",
            "destination": "osc_1_tune",
            "amount": file_doc["settings"]["modulation_1_amount"],
            "bipolar": file_doc["settings"]["modulation_1_bipolar"],
        }
    ]
    assert outcome["routes_update"] == []
    assert outcome["paths"] == {} or "settings.modulations.0.source" not in outcome["paths"]


def test_change_set_updates_an_occupied_slot_in_place():
    file_doc = preset_document(modulations=[{"source": "lfo_2", "destination": "osc_1_tune"}])
    live = preset_document(modulations=[{"source": "lfo_1", "destination": "filter_1_cutoff"}])
    outcome = PRESET.change_set(live, file_doc)
    assert outcome["routes_update"][0]["slot"] == 1
    assert outcome["routes_update"][0]["destination"] == "osc_1_tune"
    assert outcome["routes_create"] == []


def test_change_set_removes_a_pair_the_preset_leaves_inactive():
    file_doc = preset_document(modulations=[{"source": "", "destination": ""}])
    live = preset_document(modulations=[{"source": "lfo_1", "destination": "filter_1_cutoff"}])
    outcome = PRESET.change_set(live, file_doc)
    assert outcome["routes_remove"] == [{"slot": 1}]


def test_change_set_skips_a_guarded_state_path_with_a_gap():
    file_doc = preset_document(controls={"osc_1_distortion_type": 4.0})
    live = preset_document(controls={"osc_1_distortion_type": 0.0})
    outcome = PRESET.change_set(
        live, file_doc, guarded_paths=["settings.osc_1_distortion_type"]
    )
    assert "settings.osc_1_distortion_type" not in outcome["paths"]
    reasons = [gap["reason"] for gap in outcome["gaps"]]
    assert "guarded_rule_subject" in reasons
    assert outcome["counts"]["guarded_skipped"] == 1


def test_change_set_names_an_oversized_sample_as_a_gap():
    file_doc = preset_document(sample_len=4096)
    live = preset_document(sample_len=64)
    outcome = PRESET.change_set(live, file_doc)
    assert PRESET.__name__  # silence linters about the import alias
    assert all(path != "settings.sample" for path in outcome["paths"])
    reasons = [gap["reason"] for gap in outcome["gaps"]]
    assert "sample_growth_over_budget" in reasons


def test_change_set_carries_a_same_size_sample():
    file_doc = preset_document(sample_fill=77)
    live = preset_document(sample_fill=9)
    outcome = PRESET.change_set(live, file_doc)
    assert outcome["paths"]["settings.sample"]["length"] == 64


def test_change_set_names_an_element_too_big_for_the_write_band():
    """The corpus case (P38 acceptance, 2026-10-17): a long LFO curve cannot be carried.

    Measured live: the plugin ACCEPTS a state write whose own growth passes the band
    and then re-serializes the value unchanged - the write is silently ignored. An
    LFO whose file has more points than the live one is the common case (a 19-point
    curve over a 3-point one grows by ~+190). The element is carried WHOLE or named
    whole: a `num_points` that landed while its `points` array did not would leave
    the live curve in a state the preset never had.
    """
    live = preset_document()
    long_lfo = {
        "num_points": 19,
        "points": [round(0.05 * index, 4) for index in range(38)],
        "powers": [0.0] * 19,
    }
    file_doc = preset_document()
    file_doc["settings"]["lfos"] = [long_lfo]
    outcome = PRESET.change_set(live, file_doc)
    assert [path for path in outcome["paths"] if path.startswith("settings.lfos.")] == []
    assert outcome["counts"]["lfos"] == 0
    assert outcome["counts"]["growth_over_budget"] == 1
    gaps = [gap for gap in outcome["gaps"] if gap["reason"] == "growth_over_budget"]
    assert len(gaps) == 1
    assert gaps[0]["item"] == "settings.lfos.0"
    assert gaps[0]["field"].endswith(".points")
    assert gaps[0]["growth_chars"] > gaps[0]["limit_chars"]


def test_change_set_carries_a_small_element_whole():
    live = preset_document()
    short = {"num_points": 2, "points": [0.0, 1.0, 0.0, 0.25], "powers": [0.0, 0.0]}
    file_doc = preset_document()
    file_doc["settings"]["lfos"] = [short]
    outcome = PRESET.change_set(live, file_doc)
    carried = {path for path in outcome["paths"] if path.startswith("settings.lfos.")}
    # the live element's `powers` already matches, so only the differing fields ride
    assert carried == {"settings.lfos.0.num_points", "settings.lfos.0.points"}
    assert outcome["counts"]["lfos"] == 2
    assert outcome["counts"]["growth_over_budget"] == 0


def test_change_set_skips_a_destination_the_build_was_measured_to_drop():
    """A routing the plugin drops is left out UP FRONT, and named (P38 2026-10-17).

    Measured live: the host accepts a pair whose destination the running build no
    longer carries and the plugin re-serialises it out - the slot reads back empty.
    Writing it again would repeat the same apply the plugin discards, so a memo of
    the destinations THIS build dropped (keyed by the live synth version) turns the
    second apply into a plain skip with the measurement behind it.
    """
    file_doc = preset_document(modulations=[{"source": "lfo_1", "destination": "osc_2_spectral_morph_amount"}])
    live = preset_document(modulations=[{"source": "", "destination": ""}])
    outcome = PRESET.change_set(
        live, file_doc, dropped_destinations=["osc_2_spectral_morph_amount"]
    )
    assert outcome["routes_create"] == []
    assert outcome["counts"]["routes_skipped_known_drop"] == 1
    gaps = [gap for gap in outcome["gaps"] if gap["reason"] == "destination_dropped_by_plugin"]
    assert gaps and gaps[0]["destination"] == "osc_2_spectral_morph_amount"


def test_a_destination_that_is_not_in_the_memo_is_still_written():
    file_doc = preset_document(modulations=[{"source": "lfo_1", "destination": "osc_2_tune"}])
    live = preset_document(modulations=[{"source": "", "destination": ""}])
    outcome = PRESET.change_set(live, file_doc, dropped_destinations=["osc_2_spectral_morph_amount"])
    assert [row["destination"] for row in outcome["routes_create"]] == ["osc_2_tune"]
    assert outcome["counts"]["routes_skipped_known_drop"] == 0


def test_the_drops_memo_round_trips_and_is_keyed_by_build(tmp_path: Path):
    rows = [{"source": "lfo_1", "destination": "osc_2_spectral_morph_amount", "slot": 4}]
    first = PRESET.record_dropped_destinations(
        tmp_path, rows, synth_version="1.5.5", preset_id="User/Presets/x.vital", stamp="2026-10-17T00:00:00Z"
    )
    assert first["recorded"] == ["osc_2_spectral_morph_amount"]
    assert PRESET.known_dropped_destinations(tmp_path, "1.5.5") == ["osc_2_spectral_morph_amount"]
    # a different build may know the name again: the skip is not trusted across it
    assert PRESET.known_dropped_destinations(tmp_path, "1.5.6") == []
    assert PRESET.known_dropped_destinations(tmp_path, "") == []
    # a repeat measurement counts up and keeps the first sighting
    again = PRESET.record_dropped_destinations(
        tmp_path, rows, synth_version="1.5.5", preset_id="User/Presets/y.vital", stamp="2026-10-17T01:00:00Z"
    )
    assert again["recorded"] == ["osc_2_spectral_morph_amount"]
    table = PRESET.read_dropped_destinations(tmp_path)["destinations"]
    assert table["osc_2_spectral_morph_amount"]["count"] == 2
    assert table["osc_2_spectral_morph_amount"]["first_seen"] == "2026-10-17T00:00:00Z"
    assert table["osc_2_spectral_morph_amount"]["last_preset"] == "User/Presets/y.vital"
    assert list(Path(tmp_path).glob(".*tmp*")) == [], "the memo write is atomic, never a stray temp file"


def test_an_unreadable_drops_memo_degrades_to_nothing_known(tmp_path: Path):
    assert PRESET.known_dropped_destinations(tmp_path / "absent", "1.5.5") == []
    (tmp_path / PRESET.DROPS_FILE).write_text("{not json", encoding="utf-8")
    assert PRESET.known_dropped_destinations(tmp_path, "1.5.5") == []
    (tmp_path / PRESET.DROPS_FILE).write_text(json.dumps({"destinations": "nope"}), encoding="utf-8")
    assert PRESET.known_dropped_destinations(tmp_path, "1.5.5") == []


def test_change_set_declines_a_growing_wavetable_slot(tmp_path: Path):
    table = {
        "name": "T",
        "version": "1.5.5",
        "groups": [
            {
                "components": [
                    {
                        "type": "Wave Source",
                        "keyframes": [
                            {"position": 0.0, "wave_data": pcm([0] * 2048)},
                            {"position": 0.5, "wave_data": pcm([1] * 2048)},
                        ],
                    }
                ]
            }
        ],
    }
    live_table = {
        "name": "T",
        "version": "1.5.5",
        "groups": [
            {
                "components": [
                    {
                        "type": "Wave Source",
                        "keyframes": [{"position": 0.0, "wave_data": pcm([0] * 2048)}],
                    }
                ]
            }
        ],
    }
    file_doc = preset_document()
    file_doc["settings"]["wavetables"] = [table]
    live = preset_document()
    live["settings"]["wavetables"] = [live_table]
    outcome = PRESET.change_set(live, file_doc, data_dir_path=tmp_path, preset_id="User/Presets/x.vital")
    assert outcome["wavetables"] == []
    assert "wavetable_growth_refused" in [gap["reason"] for gap in outcome["gaps"]]


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_save_writes_sorted_single_line_json(tmp_path: Path):
    saved = PRESET.save_document(preset_document(), name="MyPatch", root=tmp_path)
    path = Path(saved["path"])
    assert path.parent == tmp_path / "User" / "Presets"
    text = path.read_text(encoding="utf-8")
    assert "\n" not in text
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)
    assert saved["id"] == "User/Presets/MyPatch.vital"
    assert saved["suffix"] == 0


def test_save_auto_suffixes_on_collision(tmp_path: Path):
    first = PRESET.save_document(preset_document(), name="Patch", root=tmp_path)
    second = PRESET.save_document(preset_document(), name="Patch", root=tmp_path)
    third = PRESET.save_document(preset_document(), name="Patch", root=tmp_path)
    assert Path(first["path"]).name == "Patch.vital"
    assert Path(second["path"]).name == "Patch_2.vital"
    assert Path(third["path"]).name == "Patch_3.vital"
    assert second["suffix"] == 2


def test_save_refuses_an_unsafe_name(tmp_path: Path):
    for bad in ("a/b", "a\\b", "a:b", "..", "  "):
        with pytest.raises(InvalidValue):
            PRESET.save_document(preset_document(), name=bad, root=tmp_path)


def test_save_bank_folder_is_one_segment(tmp_path: Path):
    saved = PRESET.save_document(preset_document(), name="P", bank="MyBank", root=tmp_path)
    assert Path(saved["path"]).parent == tmp_path / "User" / "Presets" / "MyBank"


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def build_library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    write_preset(root, "User/Presets/bass_deep.vital", preset_document(name="Deep Bass", author="Ann", style="Bass"))
    write_preset(root, "User/Presets/lead_bright.vital", preset_document(name="Bright", author="Bob", style="Lead"))
    write_preset(root, "User/Presets/sub/deep_pad.vital", preset_document(name="Deep Pad", author="Ann", style="Pad"))
    return root


def test_catalog_scans_searches_and_paginates(tmp_path: Path):
    root = build_library(tmp_path)
    catalog = CAT.Catalog(root, directory=tmp_path / "cache")
    result = catalog.search(query="deep")
    assert result["total"] == 2
    assert {row["id"] for row in result["rows"]} == {
        "User/Presets/bass_deep.vital",
        "User/Presets/sub/deep_pad.vital",
    }
    assert result["has_more"] is False
    page = catalog.search(limit=1, offset=1)
    assert len(page["rows"]) == 1 and page["has_more"] is True
    by_style = catalog.search(style="lead")
    assert [row["id"] for row in by_style["rows"]] == ["User/Presets/lead_bright.vital"]
    by_author = catalog.search(query="ann")
    assert by_author["total"] == 2
    assert sorted(by_style["styles"]) == ["Bass", "Lead", "Pad"]


def test_catalog_cache_is_used_and_rebuilt_when_the_library_moves(tmp_path: Path):
    root = build_library(tmp_path)
    cache_dir = tmp_path / "cache"
    first = CAT.Catalog(root, directory=cache_dir)
    first.rows()
    assert first.status()["source"] == "scan"
    assert first.cache_file.is_file()
    second = CAT.Catalog(root, directory=cache_dir)
    second.rows()
    assert second.status()["source"] == "cache"
    # a new file moves the digest and forces a rebuild
    write_preset(root, "User/Presets/new_one.vital", preset_document(name="New"))
    third = CAT.Catalog(root, directory=cache_dir)
    rows = third.rows()
    assert third.status()["source"] == "scan"
    assert len(rows) == 4


def test_catalog_revision_mismatch_forces_a_rebuild(tmp_path: Path, monkeypatch):
    root = build_library(tmp_path)
    cache_dir = tmp_path / "cache"
    CAT.Catalog(root, directory=cache_dir).rows()
    payload = json.loads((cache_dir / "vital_catalog.json").read_text(encoding="utf-8"))
    payload["revision"] = "an-older-reader"
    (cache_dir / "vital_catalog.json").write_text(json.dumps(payload), encoding="utf-8")
    again = CAT.Catalog(root, directory=cache_dir)
    again.rows()
    assert again.status()["source"] == "scan"


def test_catalog_limit_is_bounded(tmp_path: Path):
    root = build_library(tmp_path)
    catalog = CAT.Catalog(root, directory=tmp_path / "cache")
    with pytest.raises(InvalidValue):
        catalog.search(limit=0)
    with pytest.raises(InvalidValue):
        catalog.search(offset=-1)


# ---------------------------------------------------------------------------
# The capability object
# ---------------------------------------------------------------------------


def test_capability_plan_and_info(tmp_path: Path):
    root = build_library(tmp_path)
    capability = PRESET.PresetCapability(root=root)
    outcome = capability.plan(preset_document(), "User/Presets/bass_deep.vital")
    assert outcome["preset"]["id"] == "User/Presets/bass_deep.vital"
    assert outcome["preset"]["name"] == "bass_deep"
    assert outcome["preset"]["embedded_name"] == "Deep Bass"
    assert outcome["preset"]["sha256"]
    info = capability.info("User/Presets/bass_deep.vital")
    assert info["author"] == "Ann"
    assert info["keys"]["controls"] >= 2
    search = capability.search(query="bright")
    assert search["total"] == 1


def test_catalog_dir_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(CAT.CATALOG_DIR_ENV, str(tmp_path / "cat"))
    assert CAT.catalog_directory() == (tmp_path / "cat").resolve()
    monkeypatch.setenv(CAT.CATALOG_DIR_ENV, "")
    with pytest.raises(InvalidValue):
        CAT.catalog_directory()


def test_the_tool_spec_is_declared():
    from reaper_reapy_mcp.plugins import tools as plugin_tools

    assert "plugin_presets" in plugin_tools.TOOL_NAMES
    spec = plugin_tools.SPEC["plugin_presets"]
    assert spec["destructive"] is True
    assert spec["args"]["dry_run"]["default"] is True
    assert spec["mutating_ops"] == ("apply", "save", "init")
    assert spec["args"]["op"]["enum"] == ["search", "info", "apply", "save", "init"]


def test_preset_extension_constant():
    assert PRESET.PRESET_EXT == ".vital"
    assert os.path.sep in str(Path("a") / "b")
