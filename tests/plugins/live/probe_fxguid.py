"""FX-GUID repopulation measurement probe - a LIVE MUTATION, gated and NOT run.

The V3 identity contract needs one measured fact ([02 §19.8], [19 §7 L-3]):
does `TrackFX_GetFXGUID` change when an FX slot is re-populated within a
session? Until that is measured, an identical-build replacement in the same slot
may be indistinguishable, and the contract fails closed where it cannot
distinguish.

**This probe is deliberately NOT executed during P0-P3.** Removing and re-adding
an FX is a live mutation of the user's project, which the P8 gate governs; the
P0-P3 brief forbids it outright. Running this file without every gate below
exits 2 and touches nothing.

Gates (all required):
  --i-authorize-live-mutation      explicit acknowledgement
  --project-name <name>            must equal the OPEN project's name exactly
  --slot <track>:<fx>              the designated test slot
  --plugin-name <name>             the plugin to re-add after removal
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # so `live_harness` is importable when run as a script
    sys.path.insert(0, str(HERE))

from live_harness import EVIDENCE_DIR  # noqa: E402 - run artifacts, never the repo (P32)

REFUSAL = (
    "REFUSED: this probe removes and re-adds an FX (a live mutation). It requires "
    "--i-authorize-live-mutation, a matching --project-name, --slot and --plugin-name, "
    "and it belongs to the P8 live-write window (19 Decision 4 / 14 global gates). "
    "Nothing was read, changed or written."
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--i-authorize-live-mutation", action="store_true")
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--slot", default=None, help="<track_index>:<fx_index>")
    parser.add_argument("--plugin-name", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    gates = [args.i_authorize_live_mutation, args.project_name, args.slot, args.plugin_name]
    if not all(gates):
        print(REFUSAL)
        return 2

    try:
        import reapy  # noqa: PLC0415 - the probe is a live tool by construction

        from reapy import reascript_api as R  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSED: the host client is not importable: {exc}")
        return 2

    try:
        reapy.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSED: the host is not reachable: {exc}")
        return 2

    project_name = str(R.GetProjectName(0, "", 512)[0])
    if project_name != args.project_name:
        print(
            f"REFUSED: the open project is {project_name!r}, not the designated "
            f"{args.project_name!r}. The probe only runs against its test project."
        )
        return 2

    track_index, fx_index = (int(part) for part in str(args.slot).split(":"))
    track = R.GetTrack(0, track_index)
    before = str(R.TrackFX_GetFXGUID(track, fx_index))
    removed = bool(R.TrackFX_Delete(track, fx_index))
    added = int(R.TrackFX_AddByName(track, str(args.plugin_name), False, -1))
    after = str(R.TrackFX_GetFXGUID(track, added)) if added >= 0 else None

    def _digest(value: str | None) -> str | None:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else None

    evidence = {
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe": "fxguid_repopulation",
        "project": project_name,
        "slot": {"track_index": track_index, "fx_index": fx_index},
        "plugin_name": args.plugin_name,
        # one-way digests only: a raw host pointer is never serialized (05 §5)
        "guid_before_sha256_prefix": _digest(before),
        "removed": removed,
        "readded_index": added,
        "guid_after_sha256_prefix": _digest(after),
        "guid_changed": (before != after) if after is not None else None,
        "interpretation": (
            "guid_changed=True means a re-populated slot can be distinguished by the GUID "
            "as session evidence; False means identical-build replacement is indistinguishable "
            "and the contract must keep failing closed (L-3)."
        ),
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = evidence["utc"].replace(":", "")
    path = EVIDENCE_DIR / f"fxguid_repopulation__{stamp}.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"evidence": str(path), "guid_changed": evidence["guid_changed"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
