"""P38: the measured write laws of the Vital state channel (offline).

Every law here was measured live on the 1.5.5 build (2026-10-16/17) - see the
research report `docs/universal-plugin-mcp-v3/21-vital-preset-browser-research.md`
§10. The tests pin:

* growth-budgeted batching (one big unit silently does NOT land; batches do),
* the wavetable slot owning its unit,
* the sample block's growth guard (dropped with a NAMED notice, never silently),
* the sample verification row (alignment up to the measured reload delay),
* the restore covering labels and the whole sample block (pre-advanced).
"""

from __future__ import annotations

import base64
import copy
import json
import struct
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

from reaper_reapy_mcp.plugins.errors import InvalidValue  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402


def pcm(values: list[int]) -> str:
    return base64.b64encode(struct.pack("<%dh" % len(values), *values)).decode("ascii")


def sample_block(length: int = 64, fill: int = 100) -> dict:
    values = [fill + (index % 7) for index in range(length)]
    return {"name": "UnitTest", "length": length, "sample_rate": 44100, "samples": pcm(values)}


def document(**settings_overrides) -> dict:
    settings = {
        "osc_1_level": 0.5,
        "filter_1_cutoff": 60.0,
        "env_1_attack": 0.1,
        "sample": sample_block(),
        "modulations": [
            {"source": "lfo_1", "destination": "filter_1_cutoff"},
            {"source": "", "destination": ""},
        ],
        "lfos": [{"num_points": 3, "points": [0.0, 0.0, 1.0, 0.5], "powers": [0.0, 0.0]}],
        "custom_warps": [{"name": "A", "num_points": 3, "points": [0.0, 1.0, 0.5], "powers": [0.0, 0.0]}],
        "random_values": [{"seed": 1}],
        "wavetables": [],
    }
    settings.update(settings_overrides)
    return {
        "synth_version": "1.5.5",
        "preset_name": "",
        "author": "",
        "comments": "",
        "preset_style": "",
        "macro1": "MACRO 1",
        "macro2": "MACRO 2",
        "macro3": "MACRO 3",
        "macro4": "MACRO 4",
        "settings": settings,
    }


def codec() -> VC.VitalCodec:
    return VC.VitalCodec()


# ---------------------------------------------------------------------------
# Growth-budgeted batching
# ---------------------------------------------------------------------------


def test_many_changed_paths_are_split_so_no_unit_grows_past_the_budget():
    """The measured law: one big unit does not land; batches do."""
    doc = document()
    changes = {
        "paths": {
            f"settings.env_{index % 6 + 1}_attack": 0.1234567890123 + index for index in range(40)
        }
    }
    plan = codec().plan_state_writes(doc, changes)
    assert len(plan.units) > 1, "40 changed paths must not ride one unit (measured: +168 did not land)"
    for unit in plan.units:
        growth = sum(
            VC._growth_of(doc, path, value) for path, value in (unit.payload.get("paths") or {}).items()
        )
        assert growth <= VC.BATCH_GROWTH_BUDGET + 30, growth
    assert [unit.order_index for unit in plan.units] == list(range(len(plan.units)))


def test_no_path_is_dropped_by_the_batcher():
    doc = document()
    paths = {f"settings.env_{index % 6 + 1}_attack": float(index) / 100.0 for index in range(77)}
    plan = codec().plan_state_writes(doc, {"paths": paths})
    written = {
        path for unit in plan.units for path in (unit.payload.get("paths") or {})
    }
    assert written == set(paths)


def test_a_single_path_change_stays_one_unit():
    doc = document()
    plan = codec().plan_state_writes(doc, {"paths": {"settings.osc_1_level": 0.75}})
    assert len(plan.units) == 1
    assert plan.units[0].payload["paths"] == {"settings.osc_1_level": 0.75}


def test_a_path_inside_the_band_is_never_dropped_by_the_packer():
    """A change the single-path rule lets through is written: batching is the packer's
    business, and a path under the limit must never be named as a gap."""
    doc = document()
    small = "x" * (VC.SINGLE_PATH_GROWTH_LIMIT - 4)
    plan = codec().plan_state_writes(
        doc, {"paths": {"settings.osc_1_level": 0.5, "preset_name": small}}
    )
    written = {
        path for unit in plan.units for path in (unit.payload.get("paths") or {})
    }
    assert written == {"settings.osc_1_level", "preset_name"}
    assert plan.notices == []


def test_a_lone_path_above_the_band_is_named_instead_of_sent():
    """Measured (P38 acceptance, 2026-10-17): one path can be too big to land.

    The packing rule alone used to send such a path alone, which cannot work - the
    write is accepted and the plugin silently ignores it, so verification would fail
    and roll back everything that DID land. A path above the measured single-write
    limit is therefore dropped here and named, exactly like the sample payload. The
    corpus case: `settings.lfos.N.points` arrays (+140..+372) and a 725-character
    `comments` label, all of which read back unchanged on the live instance.
    """
    doc = document()
    huge = "x" * (VC.SINGLE_PATH_GROWTH_LIMIT + 200)
    plan = codec().plan_state_writes(
        doc, {"paths": {"settings.osc_1_level": 0.5, "comments": huge}}
    )
    written = {
        path for unit in plan.units for path in (unit.payload.get("paths") or {})
    }
    assert written == {"settings.osc_1_level"}
    assert [notice["path"] for notice in plan.notices] == ["comments"]
    assert plan.notices[0]["reason"] == "growth_over_budget"
    assert plan.notices[0]["budget_chars"] == VC.SINGLE_PATH_GROWTH_LIMIT
    assert plan.notices[0]["growth_chars"] > VC.SINGLE_PATH_GROWTH_LIMIT


# ---------------------------------------------------------------------------
# The sample block
# ---------------------------------------------------------------------------


def test_sample_block_of_equal_size_is_carried():
    doc = document()
    replacement = sample_block(fill=7)
    plan = codec().plan_state_writes(doc, {"paths": {VC.SAMPLE_PATH: replacement}})
    paths = {path for unit in plan.units for path in (unit.payload.get("paths") or {})}
    assert VC.SAMPLE_PATH in paths
    assert plan.notices == []


def test_a_growing_sample_block_is_dropped_with_a_named_notice():
    """Measured: a write growing the document past the band silently does not land.

    The codec therefore declines it BEFORE the write and names the gap - an
    attempt would fail verification and roll the whole transaction back.
    """
    doc = document()
    bigger = sample_block(length=4096, fill=3)
    plan = codec().plan_state_writes(doc, {"paths": {VC.SAMPLE_PATH: bigger}})
    paths = {path for unit in plan.units for path in (unit.payload.get("paths") or {})}
    assert VC.SAMPLE_PATH not in paths
    assert [notice["reason"] for notice in plan.notices] == ["growth_over_budget"]
    assert plan.notices[0]["path"] == VC.SAMPLE_PATH
    assert plan.notices[0]["growth_chars"] > VC.BATCH_GROWTH_BUDGET


def test_sample_verify_row_reports_the_measured_reload_delay():
    written = sample_block()
    values = list(struct.unpack("<%dh" % 64, base64.b64decode(written["samples"])))
    # the plugin's reload DELAYS the payload: first 4 samples zeroed, the rest shifted right
    shifted = pcm([0, 0, 0, 0] + values[:-4])
    read_back = dict(written)
    read_back["samples"] = shifted
    row = VC._sample_row(VC.SAMPLE_PATH, written, read_back, 1e-5)
    assert row["agrees"] is True
    assert row["payload_compared"]["shift"] == -VC.SAMPLE_RELOAD_DELAY
    assert row["unverifiable"]["leaf"] == f"{VC.SAMPLE_PATH}.samples"


def test_sample_verify_row_fails_on_a_different_payload():
    written = sample_block(fill=100)
    read_back = sample_block(fill=-100)
    row = VC._sample_row(VC.SAMPLE_PATH, written, read_back, 1e-5)
    assert row["agrees"] is False


def test_sample_verify_row_is_exact_when_the_payload_matches():
    written = sample_block()
    row = VC._sample_row(VC.SAMPLE_PATH, written, copy.deepcopy(written), 1e-5)
    assert row["agrees"] is True
    assert "unverifiable" not in row


# ---------------------------------------------------------------------------
# Wavetable slots own their unit
# ---------------------------------------------------------------------------


def test_a_wavetable_slot_rides_its_own_unit():
    table = {
        "name": "T",
        "author": "",
        "version": "1.5.5",
        "remove_all_dc": True,
        "full_normalize": False,
        "groups": [
            {
                "components": [
                    {
                        "type": "Wave Source",
                        "interpolation_style": 0,
                        "keyframes": [{"position": 0.0, "wave_data": pcm([0] * 2048)}],
                    }
                ]
            }
        ],
    }
    doc = document(wavetables=[table])
    plan = codec().plan_state_writes(
        doc,
        {
            "paths": {
                "settings.osc_1_level": 0.6,
                "settings.wavetables.0": table,
            }
        },
    )
    table_units = [unit for unit in plan.units if unit.payload.get("wavetable")]
    assert len(table_units) == 1
    assert set(table_units[0].payload["paths"]) == {"settings.wavetables.0"}


# ---------------------------------------------------------------------------
# Labels and restore
# ---------------------------------------------------------------------------


def test_labels_are_writable_state_paths():
    doc = document()
    for label in VC.LABEL_PATHS:
        VC.set_path(doc, label, "value")
    assert doc["preset_name"] == "value"
    with pytest.raises(InvalidValue):
        VC.set_path(doc, "not_a_label", "value")


def test_restore_covers_labels_and_the_whole_sample_block():
    captured = document()
    captured["preset_name"] = "Original"
    captured["macro1"] = "M1"
    snapshot = codec().snapshot_document(captured)
    live = copy.deepcopy(captured)
    live["preset_name"] = "Changed"
    live["macro1"] = "OTHER"
    live["settings"]["sample"] = sample_block(fill=50)
    changes = codec().restore_changes(live, snapshot)
    paths = changes["paths"]
    assert paths["preset_name"] == "Original"
    assert paths["macro1"] == "M1"
    assert VC.SAMPLE_PATH in paths
    # the captured payload is pre-advanced by the measured reload delay, so the
    # landed sample reproduces the captured timing (measured: byte-exact restore)
    restored = paths[VC.SAMPLE_PATH]["samples"]
    original = captured["settings"]["sample"]["samples"]
    assert restored != original
    values = list(struct.unpack("<%dh" % 64, base64.b64decode(restored)))
    assert values[-VC.SAMPLE_RELOAD_DELAY:] == [0] * VC.SAMPLE_RELOAD_DELAY
    # the derived payload leaf still never rides a restore
    assert f"{VC.SAMPLE_PATH}.samples" not in paths
    assert changes["derived_excluded"] == [f"{VC.SAMPLE_PATH}.samples"]


def test_restore_skips_labels_that_already_match():
    captured = document()
    captured["author"] = "Same"
    snapshot = codec().snapshot_document(captured)
    changes = codec().restore_changes(copy.deepcopy(captured), snapshot)
    assert "author" not in changes["paths"]


def test_the_growth_constants_stay_inside_the_measured_landing_band():
    """Non-vacuity, and the reason the budget is 24 rather than 12 or 36.

    The estimate is not the block delta: measured live (calibration probe), a single
    control estimated at 14..16 chars lands at a real delta of 20..24, i.e. the real
    delta is ~1.4x the estimate while the measured cliff sits between +44 (lands) and
    +48 (silently ignored). Both packing constants must convert to a delta under that
    cliff - the OLD values (12 and 36) failed this on opposite sides: 12 packed one
    control per unit, 36 converts to ~50 and would land an ignored write.
    """
    assert 0 < VC.BATCH_GROWTH_BUDGET * VC.GROWTH_ESTIMATE_TO_REAL <= VC.MEASURED_LANDING_BAND
    assert 0 < VC.SINGLE_PATH_GROWTH_LIMIT * VC.GROWTH_ESTIMATE_TO_REAL <= VC.MEASURED_LANDING_BAND
    assert VC.BATCH_GROWTH_BUDGET >= VC.SINGLE_PATH_GROWTH_LIMIT, (
        "a path the single-path rule lets through must be packable"
    )
    assert VC.BATCH_MAX_PATHS >= 1
    assert VC.SAMPLE_RELOAD_DELAY == 4
