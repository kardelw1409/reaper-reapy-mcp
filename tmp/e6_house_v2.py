"""Live apply of the enriched house bundle into a NEW project tab."""

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

SAVE_AS = str(REPO / "tmp" / "manifest_house_v2.rpp")


def brief(project) -> dict:
    marks = _enumerate_marks(project)
    regions = [m for m in marks if m["is_region"]]
    items = {}
    for track in project.tracks:
        files = []
        for i in range(track.n_items):
            item = RPR.GetTrackMediaItem(track.id, i)
            pos = RPR.GetMediaItemInfo_Value(item, "D_POSITION")
            length = RPR.GetMediaItemInfo_Value(item, "D_LENGTH")
            take = RPR.GetActiveTake(item)
            notes = RPR.MIDI_CountEvts(take, 0, 0, 0)
            files.append({"pos": round(float(pos), 3), "len": round(float(length), 3), "midi_notes": notes[2] if isinstance(notes, list) and len(notes) > 2 else 0})
        items[str(track.name)] = files
    return {
        "id": repr(project.id),
        "tracks": len(project.tracks),
        "track_names": [str(t.name) for t in project.tracks],
        "bpm": project.bpm,
        "meter": list(_read_time_signature(project)[:2]),
        "regions": [(m["start_s"], m["end_s"]) for m in regions],
        "items": items,
    }


def main() -> None:
    reapy.connect()
    user_project = reapy.Project()
    user_before = brief(user_project)
    print("USER BEFORE:", json.dumps({k: user_before[k] for k in ("id", "tracks", "bpm")}, ensure_ascii=False))

    RPR.Main_OnCommand(41929, 0)
    fresh = reapy.Project()
    assert repr(fresh.id) != user_before["id"], "no new tab"
    print("FRESH TAB:", repr(fresh.id), "tracks:", len(fresh.tracks))

    mcp = create_server(debug=False)

    async def run() -> dict:
        _content, structured = await mcp.call_tool(
            "apply_audiocpp_manifest",
            {"bundle_dir": "latest", "dry_run": False, "clean": False, "save_as": SAVE_AS},
        )
        return structured["result"]

    result = asyncio.run(run())
    print("TOOL:", json.dumps(
        {
            "ok": result.get("ok"),
            "status": result.get("status"),
            "message": result.get("message"),
            "applied": result.get("applied"),
            "region_clamps": result.get("region_clamps"),
            "region_overhangs": result.get("region_overhangs"),
            "regions_skipped": result.get("regions_skipped"),
            "verification": result.get("verification"),
            "warnings": result.get("warnings"),
        },
        ensure_ascii=False,
        indent=2,
    ))

    after = brief(reapy.Project())
    print("NEW TAB AFTER:", json.dumps(after, ensure_ascii=False, indent=2))

    user_after = brief(user_project)
    print("USER AFTER:", json.dumps(
        {
            "id_unchanged": user_after["id"] == user_before["id"],
            "tracks": user_after["tracks"],
            "bpm": user_after["bpm"],
            "regions": user_after["regions"],
        },
        ensure_ascii=False,
    ))

    save_path = Path(SAVE_AS)
    print("saved rpp:", save_path.exists(), save_path.stat().st_size if save_path.exists() else 0, "bytes")


if __name__ == "__main__":
    main()
