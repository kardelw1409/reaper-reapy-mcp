"""Pipeline test: bundle -> NEW project tab -> apply -> independent verification.

Read-only on the user's project (held by pointer and re-read at the end).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import reapy  # noqa: E402
from reapy import reascript_api as RPR  # noqa: E402

from reaper_reapy_mcp.manifest.apply import _enumerate_marks, _read_time_signature  # noqa: E402
from reaper_reapy_mcp.server import create_server  # noqa: E402

SAVE_AS = str(REPO / "tmp" / "manifest_house_afterglow.rpp")


def brief(project) -> dict:
    marks = _enumerate_marks(project)
    regions = [m for m in marks if m["is_region"]]
    return {
        "id": repr(project.id),
        "tracks": len(project.tracks),
        "track_names": [str(t.name) for t in project.tracks],
        "bpm": project.bpm,
        "meter": list(_read_time_signature(project)[:2]),
        "regions": [(m["start_s"], m["end_s"]) for m in regions],
        "item_counts": {str(t.name): t.n_items for t in project.tracks},
    }


def main() -> None:
    reapy.connect()
    user_project = reapy.Project()
    user_before = brief(user_project)
    print("USER BEFORE:", json.dumps({k: v for k, v in user_before.items() if k != "regions"}, ensure_ascii=False))

    RPR.Main_OnCommand(41929, 0)
    fresh = reapy.Project()
    assert repr(fresh.id) != user_before["id"], "no new tab created"
    print("FRESH TAB:", json.dumps(brief(fresh), ensure_ascii=False))

    mcp = create_server(debug=False)

    async def run() -> dict:
        _content, structured = await mcp.call_tool(
            "apply_audiocpp_manifest",
            {"bundle_dir": "latest", "dry_run": False, "clean": False, "save_as": SAVE_AS},
        )
        return structured["result"]

    result = asyncio.run(run())
    print("TOOL RESULT:", json.dumps(
        {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "message": result.get("message"),
            "bundle_dir": result.get("bundle_dir"),
            "applied": result.get("applied"),
            "region_clamps": result.get("region_clamps"),
            "regions_skipped": result.get("regions_skipped"),
            "verification": result.get("verification"),
            "warnings": result.get("warnings"),
        },
        ensure_ascii=False,
        indent=2,
    ))

    after = brief(reapy.Project())
    print("NEW TAB AFTER:", json.dumps(after, ensure_ascii=False))

    user_after = brief(user_project)
    print(
        "USER AFTER:",
        json.dumps(
            {
                "id_unchanged": user_after["id"] == user_before["id"],
                "tracks": user_after["tracks"],
                "track_names_unchanged": user_after["track_names"] == user_before["track_names"],
                "bpm": user_after["bpm"],
                "regions": user_after["regions"],
            },
            ensure_ascii=False,
        ),
    )

    save_path = Path(SAVE_AS)
    print("saved rpp:", save_path, "exists:", save_path.exists(), "bytes:", save_path.stat().st_size if save_path.exists() else 0)
    report = Path((result.get("bundle_dir") or "")).parent / Path(result.get("bundle_dir") or "").name / "apply_report.json"
    print("apply_report.json:", report, "exists:", report.exists())


if __name__ == "__main__":
    main()
