"""Offline checks of the region-matching policy in apply.py.

Pinned here because the live E3 run measured that region NAMES cannot be read
back from REAPER through reapy (EnumProjectMarkers2 declares `char** nameOut`,
so the name always comes back as ""). Identity is therefore the time range, and
this suite is what keeps the rule from silently going back to a name compare.
"""

from __future__ import annotations

from reaper_reapy_mcp.manifest.apply import _matching_regions, _same_span


def _mark(start: float, end: float, *, is_region: bool = True, name: str = "", index: int = 1) -> dict:
    return {"is_region": is_region, "start_s": start, "end_s": end, "name": name, "index": index}


def _region(name: str, start: float, end: float) -> dict:
    return {"name": name, "start_s": start, "end_s": end, "source": "plan"}


def test_matches_by_time_range_even_with_empty_names():
    marks = [_mark(0.02, 11.26, name="")]
    assert _matching_regions(marks, [_region("intro", 0.02, 11.26)]) == marks


def test_one_tenth_tolerance_is_inclusive_on_both_ends():
    marks = [_mark(0.12, 11.36)]
    assert _matching_regions(marks, [_region("intro", 0.02, 11.26)])
    marks = [_mark(0.13, 11.26)]
    assert _matching_regions(marks, [_region("intro", 0.02, 11.26)]) == []


def test_title_marker_is_never_matched():
    # The manifest title is written as a plain marker (isrgn=0) at 0.0 with no end.
    marks = [_mark(0.0, 0.0, is_region=False, name="dnb_horde")]
    assert _matching_regions(marks, [_region("intro", 0.0, 11.26)]) == []


def test_unrelated_regions_are_left_alone():
    marks = [_mark(0.02, 11.26), _mark(500.0, 600.0), _mark(11.26, 50.25)]
    matched = _matching_regions(marks, [_region("intro", 0.02, 11.26)])
    assert [m["start_s"] for m in matched] == [0.02]


def test_same_span_predicate_is_symmetric():
    mark, region = _mark(0.02, 11.26), _region("intro", 0.02, 11.26)
    assert _same_span(mark, region) and _same_span(mark, region)
