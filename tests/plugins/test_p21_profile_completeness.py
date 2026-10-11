"""P21 startup-path test and Vital profile-completeness invariants.

Two acceptance items from the round's brief live here:

- **the existing MCP's startup path loads prepared profiles** - asserted on the
  real `create_server()` with a stubbed controller, including that a *failing*
  preload cannot stop the server (the preload is bounded and best-effort by
  design);
- **the host-parameter accounting is reproduced automatically** - the fixture
  registry pins the classification logic offline, and the real registry (when
  present on this machine) pins the exact arithmetic
  ``host parameters = mapped controls + MIDI-CC slots + REAPER wrapper params``
  that the completeness probe records as evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import preload as PL
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB

REAL_REGISTRY = Path(PB.DEFAULT_REGISTRY_DIR)
WRAPPER_INDEXES = (2855, 2856, 2857)


# ---------------------------------------------------------------------------
# Startup path
# ---------------------------------------------------------------------------


def test_create_server_runs_the_preload_once_and_survives_its_failure(monkeypatch):
    from reaper_reapy_mcp import server as S

    calls: list[int] = []
    monkeypatch.setattr(S, "ReaperController", lambda debug=True: object())
    monkeypatch.setattr(S, "setup_mcp_tools", lambda mcp, controller: None)
    monkeypatch.setattr(S, "setup_plugin_tools", lambda mcp: None)
    monkeypatch.setattr(PL, "preload_from_env", lambda *args, **kwargs: calls.append(1))

    S.create_server(debug=False)
    assert calls == [1], "the prepared profiles must be loaded at startup, exactly once"

    def broken(*args, **kwargs):
        raise RuntimeError("preload broke")

    monkeypatch.setattr(PL, "preload_from_env", broken)
    assert S.create_server(debug=False) is not None, (
        "a preload that cannot run must not stop the server (bounded, best-effort, "
        "no writes, no host contact)"
    )


def test_the_preload_module_touches_no_host_and_never_raises_for_a_bad_directory(tmp_path):
    report = PL.preload_from_env(
        {
            "REAPER_MCP_PLUGIN_PROVIDERS": "vital",
            "REAPER_MCP_PLUGIN_CACHE": str(tmp_path / "absent"),
        }
    )
    assert report.ran is False
    assert report.reason == "cache_directory_absent"
    assert report.files_scanned == 0


# ---------------------------------------------------------------------------
# Completeness: the classification logic (fixture registry, offline)
# ---------------------------------------------------------------------------


def _classify(host_count: int, mapped: set[int], slot_low: int, slot_high: int) -> dict[str, list[int]]:
    """The probe's classification, kept identical to it by this test."""
    classes: dict[str, list[int]] = {
        "mapped_control": [],
        "midi_cc_slot": [],
        "reaper_wrapper_parameter": [],
        "unclassified_by_the_data": [],
    }
    for index in range(host_count):
        if index in mapped:
            classes["mapped_control"].append(index)
        elif index in WRAPPER_INDEXES:
            classes["reaper_wrapper_parameter"].append(index)
        elif slot_low <= index <= slot_high:
            classes["midi_cc_slot"].append(index)
        else:
            classes["unclassified_by_the_data"].append(index)
    return classes


def test_every_host_index_gets_exactly_one_class_on_the_fixture_registry():
    registry = PB.load_registry(directory=str(Path(__file__).resolve().parent / "fixtures" / "vital_registry"))
    profile = PB.build_profile(
        registry,
        {"fx_name": PB.VITAL_FX_NAME, "class_id": PB.VITAL_CLASS_ID, "module": PB.VITAL_MODULE, "param_count": 2858},
    )
    controls = profile["controls"]
    mapped = {int(control["binding"]["index"]) for control in controls}
    classes = _classify(2858, mapped, 775, 2854)
    assert sum(len(rows) for rows in classes.values()) == 2858
    assert len(mapped) == len(controls), "every control declares its own index"
    for control in controls:
        law = (control.get("law") or {}).get("kind")
        assert law in (
            "affine",
            "enum_index",
            "exponential",
            "identity",
            "none",
            "power",
            "unmeasured",
        ), control
        # a status must exist for every control: type, authority and evidence are
        # never absent, and a control with no conversion says so through its law
        assert control.get("type") in ("number", "enum")
        assert control.get("authority") is not None
        assert (control.get("evidence") or {}).get("strength") in ("none", "weak", "strong")


@pytest.mark.skipif(not REAL_REGISTRY.is_dir(), reason="the measured Vital registry is not on this machine")
def test_the_real_registry_accounts_for_every_host_parameter():
    registry = PB.load_registry()
    controls_file = registry.data["controls"]
    correspondence = registry.data["correspondence"]
    modules_file = registry.data["modules"]

    total = int(controls_file["n_params_total"])
    real_controls = int(controls_file["n_real_controls"])
    midi_cc = int(controls_file["n_midi_cc"])
    assert total == real_controls + midi_cc == 2858
    assert modules_file["totals"] == {
        "exposed_parameters": 2858,
        "real_controls": 778,
        "midi_cc_mappings": 2080,
        "reaper_wrapper_params": 3,
    }

    rows = correspondence["rows"]
    assert len(rows) == real_controls == 778
    with_state_key = [row for row in rows if row.get("state_key")]
    assert len(with_state_key) == 775
    assert [row["index"] for row in rows if not row.get("state_key")] == list(WRAPPER_INDEXES)
    indexes = sorted(int(row["index"]) for row in rows)
    assert indexes[:775] == list(range(775))
    assert indexes[775:] == list(WRAPPER_INDEXES)
    gaps = sorted(set(range(indexes[0], indexes[-1] + 1)) - set(indexes))
    assert len(gaps) == midi_cc and (gaps[0], gaps[-1]) == (775, 2854)

    profile = PB.build_profile(
        registry,
        {"fx_name": PB.VITAL_FX_NAME, "class_id": PB.VITAL_CLASS_ID, "module": PB.VITAL_MODULE, "param_count": total},
    )
    controls = profile["controls"]
    assert len(controls) == 775
    assert len({control["id"] for control in controls}) == 775
    assert len({int(control["binding"]["index"]) for control in controls}) == 775

    classes = _classify(total, {int(control["binding"]["index"]) for control in controls}, 775, 2854)
    assert {name: len(rows) for name, rows in classes.items()} == {
        "mapped_control": 775,
        "midi_cc_slot": 2080,
        "reaper_wrapper_parameter": 3,
        "unclassified_by_the_data": 0,
    }

    # the honest "not fully described" set, pinned so a silent regression is visible.
    # P22 note: the descending derivation (2026-10-11) moved five of these rows -
    # `delay_frequency`/`phaser_frequency`/`delay_aux_frequency` became measured
    # descending exponentials and `delay_aux_tempo` a labelled enum; the four
    # `random_N_frequency` rows remain refused because their endpoint-anchored
    # exponential misses each state's *printed* precision (their `law_candidate`
    # records the measured miss). The suite pins calibration OFF, so these are the
    # registry-only numbers.
    laws: dict[str, list[str]] = {}
    for control in controls:
        laws.setdefault(str((control.get("law") or {}).get("kind")), []).append(str(control["id"]))
    assert sorted(laws.get("none", [])) == [
        "random_1_frequency",
        "random_2_frequency",
        "random_3_frequency",
        "random_4_frequency",
    ]
    assert laws.get("unmeasured", []) == []
    assert len(laws.get("exponential", [])) == 4, laws.get("exponential")
    assert len([c for c in controls if c.get("range") is None]) == 162
    assert len([c for c in controls if (c.get("evidence") or {}).get("strength") == "weak"]) == 36
    # every refused row carries a named candidate instead of silence
    candidates = {c["id"]: c["law_candidate"] for c in controls if c.get("law_candidate")}
    assert sorted(candidates) == sorted(laws.get("none", []))
    for control_id, candidate in candidates.items():
        assert candidate["kind"] == "exponential", (control_id, candidate)
        assert candidate["direction"] == "descending", (control_id, candidate)
        assert candidate["worst_state"]["ratio"] > 1.0, (control_id, candidate)


def test_registry_source_rows_equal_the_rows_load_registry_records():
    """The key-material hook and the build's provenance must be the same rows.

    `registry_source_rows` hashes the registry without parsing it, and the fast
    path trusts it for a cache-key determinant. This is the offline pin the hook
    documents: the hash-only rows, the rows `load_registry` records and the rows
    the built profile declares must be one list (a drift makes every warm call a
    loud `cache.key_material` refusal instead of a hit, so it must be caught
    here, where the message is about the code rather than about a cache).
    """
    fixture_dir = str(Path(__file__).resolve().parent / "fixtures" / "vital_registry")
    listed = PB.registry_source_rows(directory=fixture_dir)
    registry = PB.load_registry(directory=fixture_dir)
    recorded = [{"file": row["file"], "sha256": row["sha256"]} for row in registry.sources]
    assert listed == recorded
    profile = PB.build_profile(
        registry,
        {"fx_name": PB.VITAL_FX_NAME, "class_id": PB.VITAL_CLASS_ID, "module": PB.VITAL_MODULE, "param_count": 2858},
    )
    declared = [
        {"file": row["file"], "sha256": row["sha256"]}
        for row in profile["provenance"]["sources"]
    ]
    assert declared == listed


@pytest.mark.skipif(not REAL_REGISTRY.is_dir(), reason="the measured Vital registry is not on this machine")
def test_registry_source_rows_equal_on_the_measured_registry_too():
    """The same pin on the real 6.6 MB registry (the files the live key hashes)."""
    listed = PB.registry_source_rows()
    registry = PB.load_registry()
    assert listed == [{"file": row["file"], "sha256": row["sha256"]} for row in registry.sources]
    assert len(listed) == 5


def test_a_completeness_artifact_can_be_rebuilt_from_the_registry(tmp_path):
    """The report's arithmetic is derivable from the registry with no host contact."""
    registry = PB.load_registry()
    profile = PB.build_profile(
        registry,
        {"fx_name": PB.VITAL_FX_NAME, "class_id": PB.VITAL_CLASS_ID, "module": PB.VITAL_MODULE, "param_count": 2858},
    )
    payload = {
        "host_params": int(registry.data["controls"]["n_params_total"]),
        "mapped_controls": len(profile["controls"]),
        "midi_cc_slots": int(registry.data["controls"]["n_midi_cc"]),
        "wrapper_params": len(
            [row for row in registry.data["correspondence"]["rows"] if not row.get("state_key")]
        ),
    }
    assert payload["host_params"] == (
        payload["mapped_controls"] + payload["midi_cc_slots"] + payload["wrapper_params"]
    )
    # the artifact is JSON-serializable and self-describing (the probe's shape)
    text = json.dumps(payload, sort_keys=True)
    assert json.loads(text) == payload
