"""Fixture tool for the pinned host band-map fixture (not collected by the suite).

Two jobs, deliberately separated:

1. **`assemble` + `serialize` are pure.** They turn a measured grid (and the provenance rows of
   the recordings it came from) into the fixture document and its exact bytes. The offline test
   `test_p32_evidence_policy.py` pins the committed fixture against these two functions, so the
   fixture is reproducible **without any historical recording on disk** (P33 6.3).
2. **`read_recordings` touches the outside world** and is only needed to re-derive the fixture
   from the original P19 recordings (``p19_enum_bands_through_host__*.json``). Those recordings
   were removed from the repository in P33 and live in the external research archive; point at
   them explicitly with ``--search`` (or ``REAPER_MCP_EVIDENCE_SEARCH``) when re-deriving.

``extract_grids`` **refuses** when the recordings disagree with each other, because then a
fixture would be a silent pick of one run rather than the measurement.

Usage: ``py -3.11 tests/plugins/fixtures/build_vital_host_bands_fixture.py [--search ROOT]... [--out PATH]``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIXTURE_DIR = REPO / "tests" / "plugins" / "fixtures"
OUT = FIXTURE_DIR / "vital_host_bands" / "filter_1_style_grid.json"

for _path in (str(REPO / "src"), str(REPO / "tests" / "plugins")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import live_harness as H  # noqa: E402

CONTROL = "filter_1_style"

#: The host call the grid records, and the probe that produced the recordings.
HOST_CALL = "TrackFX_FormatParamValueNormalized"
PROBE_ID = "tests/plugins/live/test_p19_probe_j.py"


class ExtractionRefused(Exception):
    """The recordings do not agree; an extraction must not pick one silently."""


def grid_points(grid: list[dict]) -> list[list]:
    """Normalize a recorded grid into the fixture's ``[value, display]`` rows."""
    return [[point["normalized"], point["display"]] for point in grid]


def extract_grids(recordings: list[tuple[Path, list[dict]]]) -> list[list]:
    """Every recording's grid, or a refusal naming the disagreement."""
    grids = [grid_points(grid) for _path, grid in recordings]
    if not grids:
        raise ExtractionRefused("no recording was given")
    if not all(grid == grids[0] for grid in grids[1:]):
        raise ExtractionRefused("the recordings disagree point for point: " + ", ".join(str(p) for p, _ in recordings))
    return grids


def assemble(grids: list[list], files: list[dict]) -> dict:
    """The fixture document: the measured grid plus the provenance of its recordings."""
    if not grids:
        raise ExtractionRefused("no grid was given")
    points = [list(row) for row in grids[0]]
    labelled = sum(1 for _value, display in points if display)
    return {
        "kind": "vital-host-band-map",
        "control": CONTROL,
        "value_kind": "normalized",
        "note": (
            "The host's own labelling of filter_1_style, extracted VERBATIM from the P19 live "
            "recordings below (no value was invented). A display of \"\" is the release's unnamed "
            "tail from 0.5625 (P25); the three recordings agree point for point, so this fixture "
            "replaces the test's former 'newest file in the evidence directory' dependency."
        ),
        "recorded_by": {
            "probe": PROBE_ID,
            "host_call": HOST_CALL,
            "files": [dict(row) for row in files],
            "grids_agree": True,
            "archive": (
                "the recordings were removed from the repository in P33 and live in the external "
                "research archive (repo_cleanup/p33_*/archive); re-derive with --search when needed"
            ),
        },
        "counts": {"points": len(points), "labelled": labelled, "unlabelled": len(points) - labelled},
        "grid": points,
    }


def serialize(fixture: dict) -> str:
    """The fixture's exact text (LF bytes; the file is written with this)."""
    return json.dumps(fixture, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def provenance_rows(recordings: list[tuple[Path, list[dict]]]) -> list[dict]:
    return [
        {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "points": len(grid)}
        for path, grid in recordings
    ]


def read_recordings(roots: list[Path] | None) -> list[tuple[Path, list[dict]]]:
    """The recorded band maps, wherever they are (archive-aware; see the module docstring)."""
    found: list[tuple[Path, list[dict]]] = []
    for path in sorted(H.evidence_candidates("p19_enum_bands_through_host__*.json", roots=roots)):
        document = json.loads(path.read_text(encoding="utf-8"))
        controls = document.get("controls") or {}
        if CONTROL not in controls:
            continue
        found.append((path, controls[CONTROL]["grid"]))
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--search",
        action="append",
        default=None,
        help="a root to search for recordings (repeatable; default: harness search)",
    )
    parser.add_argument(
        "--out",
        default=str(OUT),
        help="where to write the fixture (default: the committed fixture path)",
    )
    args = parser.parse_args()
    roots = [Path(value) for value in args.search] if args.search else None
    out = Path(args.out)

    recordings = read_recordings(roots)
    if not recordings:
        print("no recording of the host band map was found; nothing written")
        return 2
    try:
        grids = extract_grids(recordings)
    except ExtractionRefused as exc:
        print(f"refused: {exc}")
        return 3

    fixture = assemble(grids, provenance_rows(recordings))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize(fixture), encoding="utf-8", newline="\n")
    print(f"{out}  {out.stat().st_size} bytes; points {fixture['counts']['points']} labelled {fixture['counts']['labelled']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
