"""The DRAWN grid must divide the quantisation the parts were snapped to.

MEASURED DEFECT this file exists for (dnb take, 2026-10-20). The user, looking at the filled
project: "looks like the BPM chosen is entirely wrong, your own kick is not on the grid at all".
The tempo was right and so were the notes - measured in that very project with reapy, all 226 kick
notes sit 0.0000 steps from the 1/16 line. What was wrong was the DRAWING: `GetSetProjectGrid`
returned division 0.25, a QUARTER note, while 106 of those 226 notes (47 %) are on ODD 16ths, i.e.
exactly halfway between the lines REAPER draws. Half the kicks therefore LOOK off the grid.

The division is in whole notes and the pipeline quantises to `bar / 16`, so the metre is part of the
answer - the same 16 steps per bar is a 1/16 note in 4/4 and a 1/32 note in 2/4. These tests are
offline: the arithmetic is pure and the host call is checked through a double.
"""

from __future__ import annotations

import pytest

from reaper_reapy_mcp.manifest import apply


def test_a_16_step_grid_is_a_16th_note_in_4_4_and_a_32nd_in_2_4():
    """The measure-relative step and the note-relative division are the same number only in 4/4.

    A 2/4 bar is HALF a whole note, so `bar / 16` is a 1/32 - the mistake that once drew every
    region one bar late, in the other direction.
    """
    assert apply.grid_division_notes(4, 4) == pytest.approx(0.0625, abs=1e-12)
    assert apply.grid_division_notes(2, 4) == pytest.approx(0.03125, abs=1e-12)
    assert apply.grid_division_notes(6, 8) == pytest.approx(0.046875, abs=1e-12)
    # the same step in a different metre must NOT come out the same
    assert apply.grid_division_notes(4, 4) != pytest.approx(apply.grid_division_notes(2, 4))


class _FakeRPR:
    """A host that remembers the division, so the read-back is a real check and not decoration."""

    def __init__(self, *, keep: bool = True) -> None:
        self.division = 0.25
        self.keep = keep

    def GetSetProjectGrid(self, project_id, set_value, division, swingmode, swingamt):  # noqa: N802
        if set_value:
            if self.keep:
                self.division = float(division)
            return [1, project_id, True, self.division, 0, 0.0]
        return [1, project_id, False, self.division, 0, 0.0]


class _Project:
    id = "proj"


def test_the_setter_reports_what_it_actually_read_back(monkeypatch):
    fake = _FakeRPR()
    monkeypatch.setattr(apply, "RPR", fake)

    report = apply._set_grid_division(_Project(), 0.0625)

    assert report["before"] == 0.25, report
    assert report["after"] == 0.0625, report
    assert report["ok"] is True, report
    assert fake.division == 0.0625


def test_a_host_that_ignores_the_value_is_REPORTED_not_assumed(monkeypatch):
    """The control: with a host that silently keeps its old value the report must say ok=False.

    Without this half, `ok` could be a constant and the test above would prove nothing about it.
    """
    fake = _FakeRPR(keep=False)
    monkeypatch.setattr(apply, "RPR", fake)

    report = apply._set_grid_division(_Project(), 0.0625)

    assert report["after"] == 0.25, report
    assert report["ok"] is False, report
    assert "did not keep" in (report.get("reason") or ""), report
