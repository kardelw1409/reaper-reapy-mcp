"""Manual fixture-refresh tool for the pinned host-calibration fixture (not in the suite).

Reads the live calibration artifact (the one the preparation probe writes) and
writes a small, self-describing **subset** into
``tests/plugins/fixtures/vital_calibration/vital_host_calibration.json``:

- the identity block is adapted to the *fixture registry's* identity
  (``param_count`` 8) so the offline pipeline can load it at all - the real
  measurement's identity stays in the live artifact and is recorded under
  ``_fixture.source_identity``;
- the control set keeps exactly the ids the offline tests need: every id the
  fixture registry declares, the four controls whose law stays `none`, the three
  measured descending exponentials, and the first few controls of the two
  families that pin unit metadata and step metadata - the rule is recorded in
  ``_fixture.selection`` so a reader can tell pinned data from generated data;
- every ``samples`` list stays exactly as measured (same values, same order,
  same grid), so a confirmation an offline test observes is a statement about
  the real host text.

Usage: ``py -3.11 tests/plugins/fixtures/build_vital_calibration_fixture.py``
(optionally ``--artifact PATH``; default is the provider's default calibration
path).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
FIXTURE_DIR = REPO / "tests" / "plugins" / "fixtures"
OUT = FIXTURE_DIR / "vital_calibration" / "vital_host_calibration.json"

sys.path.insert(0, str(SRC))
from reaper_reapy_mcp.plugins.providers.vital import calibration as CAL  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB  # noqa: E402

#: The four controls whose registry law is `none` even after the measured
#: derivation (their endpoint-anchored exponential misses the printed precision).
LAW_NONE_IDS = (
    "random_1_frequency",
    "random_2_frequency",
    "random_3_frequency",
    "random_4_frequency",
)

#: The three measured **descending** exponentials.
DESCENDING_IDS = ("delay_frequency", "phaser_frequency", "delay_aux_frequency")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    artifact_path = (
        Path(argv[argv.index("--artifact") + 1]) if "--artifact" in argv else Path(CAL.default_calibration_path())
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    source_sha = sha256(artifact_path)

    registry = PB.load_registry(directory=str(FIXTURE_DIR / "vital_registry"))
    fixture_ids = [str(row["state_key"]) for row in registry.rows() if row.get("state_key")]
    controls = artifact["controls"]
    missing = [cid for cid in fixture_ids + list(LAW_NONE_IDS) + list(DESCENDING_IDS) if cid not in controls]
    if missing:
        raise SystemExit(f"the artifact does not carry every needed control: {missing}")

    with_steps = sorted(
        cid for cid, entry in controls.items() if (entry.get("steps") or {}).get("reported")
    )[:5]
    with_percent = sorted(
        cid for cid, entry in controls.items() if entry.get("unit_token") == "%"
    )[:5]
    selected = list(
        dict.fromkeys([*fixture_ids, *LAW_NONE_IDS, *DESCENDING_IDS, *with_steps, *with_percent])
    )

    fixture = {
        "kind": artifact["kind"],
        "method_version": artifact["method_version"],
        "generated_utc": artifact["generated_utc"],
        "host": dict(artifact.get("host") or {}),
        "identity": {
            **{k: v for k, v in artifact["identity"].items()},
            "param_count": 8,
        },
        "grid": list(artifact["grid"]),
        "controls": {cid: controls[cid] for cid in sorted(selected)},
        "read_only_proof": dict(artifact.get("read_only_proof") or {}),
        "host_param_space": dict(artifact.get("host_param_space") or {}),
        "host_default_value": dict(artifact.get("host_default_value") or {}),
        "_fixture": {
            "kind": "subset",
            "source_path": str(artifact_path),
            "source_sha256": source_sha,
            "source_identity": dict(artifact["identity"]),
            "identity_note": (
                "the identity block is adapted to the fixture registry's fingerprint "
                "(param_count 8) so the offline pipeline can load this file; every "
                "`controls` entry is the measured content, unmodified"
            ),
            "selection": {
                "fixture_registry_ids": len(fixture_ids),
                "law_none_ids": len(LAW_NONE_IDS),
                "descending_ids": len(DESCENDING_IDS),
                "first_with_steps": with_steps,
                "first_with_percent_token": with_percent,
                "total_kept": len(selected),
                "of_total_controls": len(controls),
            },
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(selected)} controls kept of {len(controls)})")
    print("source:", artifact_path, "sha256:", source_sha[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
