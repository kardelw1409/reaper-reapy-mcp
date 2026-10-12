"""Manual fixture-refresh tool for the pinned enum-boundary fixture (not in the suite).

Reads the live artifact the P23 preparation probe writes
(``vital_enum_boundaries.json``) and writes a small, self-describing **subset**
into ``tests/plugins/fixtures/vital_enum_boundaries/vital_enum_boundaries.json``:

- the identity block is adapted to the *fixture registry's* fingerprint
  (``param_count`` 8) so the offline pipeline can load it; the measured identity
  stays in the live artifact and is recorded under ``_fixture.source_identity``;
- the control set keeps every id the fixture registry declares (so the merge
  meets the same controls the offline profile has) plus one control of each
  *outcome class* the tests must pin:
  ``OUTCOME_CLASSES`` below - a refined control (measured brackets that contradict
  the profile), a confirmed one (the bracket contains the profile's boundary), a
  ``continuous_display`` family member (no band structure), the ``unresolved``
  wrapper parameter, and the three unlabelled-tail controls;
- every bracket stays exactly as measured (same doubles, same states), so a
  claim an offline test makes about a boundary is a statement about the real
  host's answer at real values.

Usage: ``py -3.11 tests/plugins/fixtures/build_vital_enum_boundaries_fixture.py``
(optionally ``--artifact PATH``; default is the provider's default location).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
FIXTURE_DIR = REPO / "tests" / "plugins" / "fixtures"
OUT = FIXTURE_DIR / "vital_enum_boundaries" / "vital_enum_boundaries.json"

sys.path.insert(0, str(SRC))
from reaper_reapy_mcp.plugins.providers.vital import enum_boundaries as EB  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB  # noqa: E402

#: One control per outcome class the offline tests pin, kept by id so the fixture
#: is a *named* selection rather than "whatever the run produced".
OUTCOME_CLASSES: dict[str, tuple[str, ...]] = {
    # measured brackets that contradict the sweep-derived switch points: the
    # misread window this round closes (widest measured offset 3.3e-3)
    "refined": ("osc_1_destination", "delay_aux_sync"),
    # a two-member control whose uniform boundary (0.5) is *not* where the host
    # flips (measured: 0.5 - 1.49e-8), and one whose bands are non-uniform
    "exact_boundary": ("chorus_on", "filter_1_style"),
    # the display ramps between "members": the member list is a sampling artefact
    "continuous_display": ("filter_1_drive", "lfo_1_phase", "distortion_filter_blend"),
    # the REAPER wrapper parameter whose member ids the host never prints
    "unresolved": ("bypass",),
    # an unlabelled region above the last named member
    "unlabelled_tail": ("filter_2_style", "filter_fx_style"),
}


#: Fields the loader/merge reads from a transition. Everything else the probe
#: records (the candidate values it tested, the bisection step counts, the anchor
#: positions) is *working* - it explains how the bracket was found, and the live
#: artifact keeps it; a fixture that carried it would be several hundred KB of
#: transcription for no test to read.
_TRANSITION_FIELDS = ("i", "state", "lo", "hi", "width", "value", "lower_member", "upper_member")
_POSITION_FIELDS = ("member", "position", "text", "ok")


def slim(record: dict) -> dict:
    """The measured core of one control's record, with the trimming declared."""
    trimmed = {
        "index": record.get("index"),
        "members": list(record["members"]),
        "n_members": record.get("n_members"),
        "current_switch_points": list(record.get("current_switch_points") or []),
        "current_source": record.get("current_source"),
        "transitions": [
            {key: row[key] for key in _TRANSITION_FIELDS if key in row}
            for row in record.get("transitions") or []
        ],
        "positions_checked": [
            {key: row[key] for key in _POSITION_FIELDS if key in row}
            for row in record.get("positions_checked") or []
        ],
        "unresolved": list(record.get("unresolved") or []),
    }
    if record.get("tail") is not None:
        trimmed["tail"] = record["tail"]
    if record.get("host_step_metadata") is not None:
        trimmed["host_step_metadata"] = record["host_step_metadata"]
    if record.get("continuous_display"):
        continuous = record["continuous_display"]
        trimmed["continuous_display"] = {
            "transitions": continuous.get("transitions"),
            "fit": dict(continuous.get("fit") or {}),
            "samples": list(continuous.get("samples") or [])[:3],
            "note": continuous.get("note"),
        }
    return trimmed


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def outcome_of(record: dict) -> str:
    states = {str(row.get("state")) for row in record.get("transitions") or []}
    if (record.get("tail") or {}).get("state") == "unlabelled_region":
        return "unlabelled_tail"
    if "unresolved" in states or "foreign_label" in states:
        return "unresolved"
    if states == {"continuous_display"}:
        return "continuous_display"
    if states == {"exact_at"} or states == {"exact"}:
        return "exact_boundary" if len(record.get("members") or []) <= 2 else "refined"
    return "other"


def main(argv: list[str]) -> int:
    artifact_path = (
        Path(argv[argv.index("--artifact") + 1])
        if "--artifact" in argv
        else Path(EB.default_enum_boundaries_path())
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    source_sha = sha256(artifact_path)

    registry = PB.load_registry(directory=str(FIXTURE_DIR / "vital_registry"))
    controls = artifact["controls"]
    fixture_rows = [row for row in registry.rows() if row.get("state_key")]
    fixture_ids = [str(row["state_key"]) for row in fixture_rows]
    # the artifact carries **enum** controls only (it measures band boundaries), so a
    # fixture control with a numeric law is legitimately absent - it is reported, not
    # silently dropped
    present = [cid for cid in fixture_ids if cid in controls]
    numeric = [cid for cid in fixture_ids if cid not in controls]
    if not present:
        raise SystemExit(
            f"the artifact carries none of the fixture registry's enum controls (absent: {numeric})"
        )

    wanted = list(present)
    for label, ids in OUTCOME_CLASSES.items():
        for control_id in ids:
            if control_id not in controls:
                raise SystemExit(f"the artifact does not carry {control_id!r} ({label})")
            wanted.append(control_id)
    # a few more of each class, so the fixture covers more than the named ones
    by_class: dict[str, list[str]] = {}
    for control_id, record in controls.items():
        by_class.setdefault(outcome_of(record), []).append(control_id)
    for label, extra in (
        ("refined", 2),
        ("exact_boundary", 3),
        ("continuous_display", 1),
    ):
        wanted.extend(sorted(by_class.get(label, []))[:extra])
    # a 257-member continuous control is ~25 KB of members on its own, and its
    # *shape* is what the tests assert, not its length: keep the shortest member
    # list of that class and drop the rest
    big = [
        cid
        for cid in wanted
        if len(controls[cid]["members"]) > 64
        and {row["state"] for row in controls[cid]["transitions"]} == {"continuous_display"}
    ]
    if len(big) > 1:
        keep = min(big, key=lambda cid: len(controls[cid]["members"]))
        wanted = [cid for cid in wanted if cid not in big or cid == keep]
    selected = list(dict.fromkeys(wanted))

    fixture = {
        "kind": artifact["kind"],
        "method_version": artifact["method_version"],
        "generated_utc": artifact["generated_utc"],
        "host": dict(artifact.get("host") or {}),
        "identity": {**{k: v for k, v in artifact["identity"].items()}, "param_count": 8},
        "method": dict(artifact.get("method") or {}),
        "controls": {cid: slim(controls[cid]) for cid in sorted(selected)},
        "read_only_proof": dict(artifact.get("read_only_proof") or {}),
        "counts": dict(artifact.get("counts") or {}),
        "_fixture": {
            "kind": "subset",
            "source_path": str(artifact_path),
            "source_sha256": source_sha,
            "source_identity": dict(artifact["identity"]),
            "identity_note": (
                "the identity block is adapted to the fixture registry's fingerprint "
                "(param_count 8) so the offline pipeline can load this file; every "
                "`members` list, bracket, state, text and fit is the measured content, "
                "unmodified"
            ),
            "trimming": {
                "transition_fields_kept": list(_TRANSITION_FIELDS),
                "position_fields_kept": list(_POSITION_FIELDS),
                "dropped": [
                    "tested",
                    "unexpected",
                    "anchors",
                    "calls",
                    "steps",
                    "bracket_source",
                    "notes",
                    "positions_checked.attempts",
                ],
                "why": (
                    "the loader and the merge read the measured core; the working "
                    "fields stay in the live artifact, which is a declared profile source"
                ),
            },
            "selection": {
                "fixture_registry_ids": len(fixture_ids),
                "fixture_enum_ids": len(present),
                "fixture_numeric_ids": numeric,
                "named_outcomes": {label: list(ids) for label, ids in OUTCOME_CLASSES.items()},
                "by_class_available": {label: len(ids) for label, ids in sorted(by_class.items())},
                "total_kept": len(selected),
                "of_total_controls": len(controls),
            },
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(selected)} controls kept of {len(controls)})")
    print("classes available:", json.dumps({k: len(v) for k, v in sorted(by_class.items())}))
    print("source:", artifact_path, "sha256:", source_sha[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
