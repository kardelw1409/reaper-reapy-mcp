"""apply_audiocpp_manifest: fill the CURRENT REAPER project from a bundle.

Mutation policy (E2):
  * tracks / items / tempo          -> the ReaperController (house primitives)
  * regions / markers / save        -> direct ReaperScript calls, because
    (a) reapy.core.Region exposes no name (region identity needs
    EnumProjectMarkers2) and (b) the controller's create_region is the known
    save-lossy path; RPR.AddProjectMarker(0, 1, ...) is the validated one.
  * time signature                  -> direct ReaperScript calls as well: the
    controller's set_project_time_signature calls TimeMap_SetTimeSigAtTime, which
    does not exist in reapy's API dump (measured). See _set_time_signature.

Clean order: regions are deleted BEFORE tracks. Marker/region indices are
independent of track indices, but our matching set was computed from the
pre-mutation state, so removing the whole marker list first keeps the
"delete by recorded index" step atomic (no interleaving with track shifts).

Everything is guarded fail-closed: a malformed bundle, a second apply without
clean=true, or missing artifacts refuse before any mutation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import reapy
from reapy import reascript_api as RPR

from .manifest import (
    AI_TRACKS,
    APPLY_REPORT_FILENAME,
    ManifestError,
    clamp_regions,
    load_manifest,
    resolve_bundle_dir,
)

_MATCH_TOLERANCE_S = 0.1


def _enumerate_marks(project) -> list[dict]:
    """All markers+regions of the project with positions (reapy Region has no name).

    Measured reapy return shape of EnumProjectMarkers2:
    [retval, proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber].

    MEASURED LIMIT: the `name` element is ALWAYS empty - the C signature declares
    `char** nameOut`, which reapy cannot fill (passing "" or 0 both return '').
    The name IS stored: the region this tool creates appears in the saved .rpp as
    `MARKER 1 0.02 intro 1 0 1 R {...}`, while the same read returns ''. Every
    name-based decision here would therefore be made on an empty string, so region
    identity is (start_s, end_s) within _MATCH_TOLERANCE_S.
    """
    marks: list[dict] = []
    idx = 0
    while True:
        row = RPR.EnumProjectMarkers2(project.id, idx, 0, 0.0, 0.0, "", 0)
        if not row[0]:
            break
        marks.append(
            {
                "is_region": bool(row[3]),
                "start_s": float(row[4]),
                "end_s": float(row[5]),
                "name": row[6],  # unreliable: see the docstring above
                "index": int(row[7]),
            }
        )
        idx += 1
    return marks


def _read_time_signature(project) -> tuple[int, int, float]:
    """(num, den, bpm) at the project start. Measured shape of the 5-arg call:

    TimeMap_GetTimeSigAtTime(proj, time, numOut, denOut, bpmOut)
      -> [proj, time, timesig_num, timesig_denom, bpm]
    """
    row = RPR.TimeMap_GetTimeSigAtTime(project.id, 0.0, 0, 0, 0.0)
    return int(row[2]), int(row[3]), float(row[4])


def _set_time_signature(project, num: int, den: int, bpm: float) -> tuple[bool, str]:
    """Set the time signature at the project start. Returns (called_ok, how).

    The controller's set_project_time_signature cannot work here: it calls
    TimeMap_SetTimeSigAtTime, which does NOT exist in reapy's generated API
    (measured: "module 'reapy.reascript_api' has no attribute
    'TimeMap_SetTimeSigAtTime'", logged by the controller). The two functions that
    DO exist are used instead, each measured live:
      * no tempo/time-sig marker -> AddTempoTimeSigMarker(proj, timepos, bpm, num,
        den, lineartempo), 6 args, returned 1 and the read-back went 4/4 -> 3/4.
      * a marker exists -> SetTempoTimeSigMarker(proj, -1, timepos, -1, -1, bpm,
        num, den, lineartempo). ptidx MUST be -1 ("marker covering timepos"): with
        ptidx=0 the call returned 1 and changed NOTHING while -1 applied it.
    """
    if int(RPR.CountTempoTimeSigMarkers(project.id)) == 0:
        ok = bool(RPR.AddTempoTimeSigMarker(project.id, 0.0, bpm, num, den, False))
        return ok, "added a tempo/time-signature marker at 0.0 s"
    ok = bool(RPR.SetTempoTimeSigMarker(project.id, -1, 0.0, -1, -1, bpm, num, den, False))
    return ok, "edited the tempo/time-signature marker covering 0.0 s"


def _track_names(project) -> list[str]:
    return [str(track.name) for track in project.tracks]


def _same_span(mark: dict, region: dict) -> bool:
    """True when a project mark covers the manifest region's range (both ends)."""
    return (
        abs(mark["start_s"] - region["start_s"]) <= _MATCH_TOLERANCE_S
        and abs(mark["end_s"] - region["end_s"]) <= _MATCH_TOLERANCE_S
    )


def _matching_regions(marks: list[dict], regions: list[dict]) -> list[dict]:
    """Regions whose (start_s, end_s) match the manifest within the tolerance.

    Names cannot take part: EnumProjectMarkers2 never returns one (see
    _enumerate_marks). For regions this tool created the time range identifies
    them exactly, and it is what clean has to find.
    """
    out = []
    for mark in marks:
        if not mark["is_region"]:
            continue
        if any(_same_span(mark, region) for region in regions):
            out.append(mark)
    return out


def apply_bundle(
    bundle_dir: str | None,
    *,
    controller,
    dry_run: bool = False,
    clean: bool = False,
    save_as: str | None = None,
) -> dict:
    """Validate, (optionally preview), mutate, verify, report.

    Raises ManifestError for anything that must refuse before touching the host.
    Returns the report dict (ok is False when verification finds less than the
    requested count applied, even though the mutations ran).
    """
    bundle = resolve_bundle_dir(bundle_dir)
    info = load_manifest(bundle)

    kept, clamps, skipped = clamp_regions(info["regions"], info["duration_s"])

    project = reapy.Project()
    before = {
        "project_id": str(project.id),
        "project_name": project.name,
        "track_count": controller.get_track_count(),
        "tempo": controller.get_tempo(),
    }
    track_names = _track_names(project)
    existing_ai = [name for name in track_names if name in AI_TRACKS]
    marks = _enumerate_marks(project)
    regions_to_delete = _matching_regions(marks, kept) if clean else []

    if existing_ai and not clean:
        raise ManifestError(
            "the project already contains AI_* tracks: " + ", ".join(sorted(set(existing_ai))),
            detail="refusing a second apply into the same project",
            hints=["use clean=true to replace the previous audiocpp fill"],
        )

    plan = {
        "tracks_to_create": list(AI_TRACKS),
        "tracks_to_delete": sorted(set(existing_ai)) if clean else [],
        "regions_to_delete": [
            {"name": m["name"], "start_s": m["start_s"], "index": m["index"]} for m in regions_to_delete
        ],
        "regions_to_create": kept,
        "region_clamps": clamps,
        "region_skipped": skipped,
        "audio_item": {"track": "AI_MASTER", "file": Path(info["audio_path"]).name, "start_s": 0.0},
        "midi_items": [
            {"track": "MIDI_VOCAL", "file": Path(info["vocal_midi_path"]).name, "start_s": 0.0},
            {"track": "MIDI_INS", "file": Path(info["ins_midi_path"]).name, "start_s": 0.0},
        ],
        "tempo": info["bpm"],
        "meter": info["meter"],
        "title_marker": info["title"],
        "save_as": save_as,
    }

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "bundle_dir": info["bundle_dir"],
            "project_before": before,
            "plan": plan,
            "warnings": list(info["warnings"]),
            "apply_report_path": None,
        }

    warnings: list[str] = list(info["warnings"])
    applied = {
        "deleted_regions": [],
        "deleted_tracks": [],
        "tracks": [],
        "audio_items": 0,
        "midi_items": 0,
        "regions": 0,
        "tempo": None,
        "meter": None,
        "meter_how": None,
        "saved_as": None,
    }

    # --- clean (regions first: their recorded indices come from the pre-mutation state)
    for mark in sorted(regions_to_delete, key=lambda m: m["index"], reverse=True):
        RPR.DeleteProjectMarker(project.id, mark["index"], True)
        applied["deleted_regions"].append({"name": mark["name"], "start_s": mark["start_s"]})
    existing_indices = sorted(
        (i for i, name in enumerate(track_names) if name in AI_TRACKS), reverse=True
    )
    for index in existing_indices:
        name = track_names[index]
        project.tracks[index].delete()
        applied["deleted_tracks"].append(name)

    # --- tempo / meter / title marker
    if not controller.set_tempo(info["bpm"]):
        warnings.append("set_tempo returned False")
    applied["tempo"] = info["bpm"]
    meter_num, meter_den = info["meter"]
    meter_ok, meter_how = _set_time_signature(project, meter_num, meter_den, info["bpm"])
    if not meter_ok:
        warnings.append(f"time signature write returned false ({meter_how})")
    applied["meter"] = info["meter"]
    applied["meter_how"] = meter_how
    RPR.AddProjectMarker(project.id, False, 0.0, 0.0, info["title"], -1)

    # --- tracks
    track_index: dict[str, int] = {}
    for name in AI_TRACKS:
        track_index[name] = controller.create_track(name)
        applied["tracks"].append(name)

    # --- items
    if controller.insert_audio_item(track_index["AI_MASTER"], info["audio_path"], 0.0) in (-1, None):
        warnings.append("audio item insert failed (insert_audio_item returned -1)")
    else:
        applied["audio_items"] += 1
    if controller.insert_midi_item(track_index["MIDI_VOCAL"], info["vocal_midi_path"], 0.0) in (-1, None):
        warnings.append("vocal MIDI insert failed")
    else:
        applied["midi_items"] += 1
    if controller.insert_midi_item(track_index["MIDI_INS"], info["ins_midi_path"], 0.0) in (-1, None):
        warnings.append("instrumental MIDI insert failed")
    else:
        applied["midi_items"] += 1

    # --- regions (validated AddProjectMarker path)
    for region in kept:
        RPR.AddProjectMarker(project.id, True, region["start_s"], region["end_s"], region["name"], -1)
        applied["regions"] += 1

    # --- save
    if save_as:
        save_path = Path(save_as)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        RPR.Main_SaveProjectEx(project.id, str(save_path), 0)
        if save_path.exists():
            applied["saved_as"] = str(save_path)
        else:
            warnings.append("Main_SaveProjectEx did not produce the file on disk")

    # --- verify by re-reading
    after_names = _track_names(project)
    tracks_ok = all(name in after_names for name in AI_TRACKS)
    marks_after = _enumerate_marks(project)
    regions_after = [m for m in marks_after if m["is_region"]]
    # Matching is by time range: marker names are unreadable through reapy
    # (measured - see _enumerate_marks), so a name comparison would always read 0.
    regions_matched = sum(1 for region in kept if any(_same_span(m, region) for m in regions_after))
    items_ok = (
        project.tracks[track_index["AI_MASTER"]].n_items >= 1
        and project.tracks[track_index["MIDI_VOCAL"]].n_items >= 1
        and project.tracks[track_index["MIDI_INS"]].n_items >= 1
    )
    tempo_after = controller.get_tempo()
    meter_after = _read_time_signature(project)
    meter_applied_ok = [meter_after[0], meter_after[1]] == info["meter"]
    verification = {
        "tracks_ok": tracks_ok,
        "items_ok": items_ok,
        "regions_expected": len(kept),
        "regions_matched": regions_matched,
        "regions_read_back": len(regions_after),
        "region_identity": "start_s/end_s within 0.1 s (reapy returns no marker names)",
        "tempo_after": tempo_after,
        "meter_after": [meter_after[0], meter_after[1]],
        "meter_ok": meter_applied_ok,
    }
    ok = bool(tracks_ok and items_ok and regions_matched == len(kept) and meter_applied_ok)
    if not ok:
        warnings.append(
            f"verification shortfall: tracks_ok={tracks_ok}, items_ok={items_ok}, "
            f"regions {regions_matched}/{len(kept)}, meter {verification['meter_after']}"
        )

    report = {
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": "apply_audiocpp_manifest",
        "bundle_dir": info["bundle_dir"],
        "manifest_title": info["title"],
        "dry_run": False,
        "clean": clean,
        "project_before": before,
        "project_after": {
            "project_id": str(project.id),
            "project_name": project.name,
            "track_count": controller.get_track_count(),
            "tempo": tempo_after,
        },
        "applied": applied,
        "region_clamps": clamps,
        "regions_skipped": skipped,
        "deletion_order": "regions_then_tracks",
        "warnings": warnings,
        "verification": verification,
    }
    report_path = Path(info["bundle_dir"]) / APPLY_REPORT_FILENAME
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": ok,
        "dry_run": False,
        "bundle_dir": info["bundle_dir"],
        "project_before": before["project_name"],
        "project_after": report["project_after"]["project_name"],
        "applied": applied,
        "region_clamps": clamps,
        "regions_skipped": skipped,
        "verification": verification,
        "warnings": warnings,
        "apply_report_path": str(report_path),
    }
