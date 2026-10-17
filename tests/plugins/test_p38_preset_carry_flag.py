"""P38: the preset-carry flag — an apply never reads as "the preset is loaded" when it is not.

The transaction's own `partial` covers what the TRANSACTION refused or lost. A preset
apply can also leave parts out at PLAN time (a host-owned value, a guarded hard rule, a
wavetable or a curve the measured growth band refuses, a routing the running build
drops), and in that case the live state is not the file. These tests pin the flag and
its reason counts; the live end of it is measured by
`probe_p38_partial_dry_run.py` in the run-artifacts notes directory (dry-run only, so
nothing is written to the instance).
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGINS_SRC = Path(__file__).resolve().parents[2] / "src"
if str(PLUGINS_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGINS_SRC))

from reaper_reapy_mcp.plugins.api import _preset_carry_flag  # noqa: E402


def test_a_plan_without_gaps_leaves_the_flag_alone():
    data = {"partial": False}
    _preset_carry_flag({"gaps": []}, data)
    assert data == {"partial": False}
    assert "preset_carry" not in data


def test_any_gap_makes_the_apply_partial_with_reason_counts():
    plan_view = {
        "gaps": [
            {"reason": "host_owned_parameter", "item": "settings.beats_per_minute"},
            {"reason": "guarded_rule_subject", "item": "settings.osc_1_distortion_type"},
            {"reason": "guarded_rule_subject", "item": "settings.osc_2_distortion_type"},
            {"reason": "growth_over_budget", "item": "settings.lfos.0"},
            {"reason": "destination_dropped_by_plugin", "destination": "modulation_4_power"},
            {"reason": "destination_dropped_by_plugin", "destination": "modulation_6_amount"},
        ]
    }
    data = {"partial": False, "counts": {"controls": 113}}
    _preset_carry_flag(plan_view, data)
    assert data["partial"] is True
    assert data["preset_carry"]["gaps"] == 6
    assert data["preset_carry"]["reasons"] == {
        "host_owned_parameter": 1,
        "guarded_rule_subject": 2,
        "growth_over_budget": 1,
        "destination_dropped_by_plugin": 2,
    }
    # the flag never hides what the transaction reported
    assert data["counts"] == {"controls": 113}


def test_a_transaction_partial_is_never_downgraded_by_an_empty_gap_list():
    data = {"partial": True}
    _preset_carry_flag({"gaps": []}, data)
    assert data["partial"] is True
