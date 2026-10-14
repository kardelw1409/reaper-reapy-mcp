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
    BASE_AI_TRACKS,
    DRUM_PART_TRACK_MAP,
    MIDI_ARTIFACT_TRACK,
    MIDI_TRACK_MAP,
    STEM_TRACK_MAP,
    ManifestError,
    clamp_regions,
    compare_note_spans,
    expected_note_spans,
    load_manifest,
    note_report_ok,
    resolve_bundle_dir,
    summarize_note_phase,
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


def _last_item_position(project, track_index: int) -> float | None:
    """Position of the newest item on a track (measured, not assumed).

    RPR.GetTrack is not usable here (reapy's dump has no such attribute); the
    reapy Track object's id is what the ReaScript calls take.
    """
    track = project.tracks[track_index]
    count = int(RPR.CountTrackMediaItems(track.id))
    if count < 1:
        return None
    item = RPR.GetTrackMediaItem(track.id, count - 1)
    return float(RPR.GetMediaItemInfo_Value(item, "D_POSITION"))


def _set_audio_source_offset(project, track_index: int, offset_s: float) -> float | None:
    """Skip the render's lead-in inside the take (REAPER clamps positions at 0).

    Measured: SetMediaItemInfo_Value(item, 'D_POSITION', -0.42) returns 1 and
    changes NOTHING, so the arrival of the downbeat is expressed as a source
    offset on the take, and the item is shortened so it does not play silence
    past the source end. Returns the offset actually stored.
    """
    track = project.tracks[track_index]
    count = int(RPR.CountTrackMediaItems(track.id))
    if count < 1:
        return None
    item = RPR.GetTrackMediaItem(track.id, count - 1)
    take = RPR.GetActiveTake(item)
    if not take:
        return None
    RPR.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", float(offset_s))
    length = float(RPR.GetMediaItemInfo_Value(item, "D_LENGTH"))
    if length > offset_s:
        RPR.SetMediaItemInfo_Value(item, "D_LENGTH", length - float(offset_s))
    return float(RPR.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS"))


def _same_span(mark: dict, region: dict) -> bool:
    """True when a project mark covers the manifest region's range (both ends)."""
    return (
        abs(mark["start_s"] - region["start_s"]) <= _MATCH_TOLERANCE_S
        and abs(mark["end_s"] - region["end_s"]) <= _MATCH_TOLERANCE_S
    )


def _take_note_phases(project, track_index: int, step_s: float) -> tuple[dict, list[float]]:
    """The newest item's note span AND how far its notes sit from the project grid.

    Returns (span, deviations) where a deviation is the distance from the nearest
    multiple of `step_s`, in [-step/2, +step/2]. The median of those deviations is the
    whole item's phase: measured on a real bundle, the drum part sat 6.4 ms off the
    1/16 grid and the score-derived parts 21 ms - the reason an arrangement could look
    "almost right but not on the grid".

    Measured against REAPER 7.53 through reapy 0.10: these ReaScript calls return a
    LIST whose first element is the return value and whose remaining elements are the
    pass-through arguments followed by the outputs - `MIDI_CountEvts(take, 0, 0, 0)`
    gives [retval, take, notesOut, ccsOut, textsOut] and `MIDI_GetNote(...)` gives
    [retval, take, idx, selected, muted, startppq, endppq, chan, pitch, vel].

    Times come from MIDI_GetProjTimeFromPPQPos, which is what makes this check able to
    see a tempo defect at all: it converts through the PROJECT tempo, so a file written
    at 120 bpm inside a 172 bpm project reports its notes 1.43x too early.
    """
    track = project.tracks[track_index]
    count = int(RPR.CountTrackMediaItems(track.id))
    if count < 1:
        return {}, []
    item = RPR.GetTrackMediaItem(track.id, count - 1)
    take = RPR.GetActiveTake(item)
    if not take:
        return {}, []
    counted = RPR.MIDI_CountEvts(take, 0, 0, 0)
    if not isinstance(counted, (list, tuple)) or len(counted) < 3:
        return {}, []
    notes = int(counted[2])
    if notes < 1:
        return {"notes": 0, "first_s": None, "last_s": None}, []
    times: list[float] = []
    for index in range(notes):
        got = RPR.MIDI_GetNote(take, index, 0, 0, 0, 0, 0, 0, 0)
        if not isinstance(got, (list, tuple)) or len(got) < 6:
            continue
        times.append(float(RPR.MIDI_GetProjTimeFromPPQPos(take, float(got[5]))))
    if not times:
        return {"notes": notes, "first_s": None, "last_s": None}, []
    span = {"notes": notes, "first_s": min(times), "last_s": max(times)}
    if step_s <= 0:
        return span, []
    deviations = [((t % step_s) + step_s / 2.0) % step_s - step_s / 2.0 for t in times]
    return span, deviations


def _take_note_span(project, track_index: int) -> dict | None:
    span, _ = _take_note_phases(project, track_index, 0.0)
    return span or None


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

    # Times in the manifest are PROJECT time: the export shifted the arrangement
    # so the render's first downbeat sits on bar 1. The audio keeps its lead-in in
    # the file and skips it through a take source offset, so the sounding end of
    # the audio in project time is duration - source_offset. A bar-aligned region
    # edge may legitimately poke up to half a bar past that end, so that much
    # overhang is kept instead of clamped.
    audio_start_s = info["audio_start_s"]
    audio_source_offset_s = info["audio_source_offset_s"]
    project_duration_s = info["duration_s"] - audio_source_offset_s + audio_start_s
    grid_info = info.get("grid") or {}
    # The ruler must run at the tempo the render actually has, not the score's: on the
    # DnB take those differ by 78 ppm, which is 21 ms of drift by the end of the song.
    project_bpm = float(grid_info.get("bpm_project") or info["bpm"])
    allow_overhang_s = 0.0
    if grid_info.get("quantize") == "bar" and grid_info.get("bar_s"):
        # A bar-aligned edge may poke past the audio by almost a whole bar (the exporter snaps
        # an over-running end UP to the next bar line), so the tolerance is a bar, and a clamp
        # - if one is ever needed - lands on a bar line too instead of cutting mid-bar.
        allow_overhang_s = float(grid_info["bar_s"])
    kept, clamps, skipped, overhangs = clamp_regions(
        info["regions"], project_duration_s, allow_overhang_s=allow_overhang_s,
        snap_s=float(grid_info.get("bar_s") or 0.0),
    )

    # Track layout: the five base tracks always exist; every artifact the bundle
    # carries adds its track (vocals/instrumental FILL base tracks, drums/bass/
    # other ADD tracks). Items all sit at the arrangement origin.
    tracks_to_create: list[str] = list(BASE_AI_TRACKS)
    for stem_key, track_name in STEM_TRACK_MAP.items():
        if stem_key in info["stems"] and track_name not in tracks_to_create:
            tracks_to_create.append(track_name)
    for midi_key, track_name in MIDI_TRACK_MAP.items():
        if midi_key in info["extra_midi"] and track_name not in tracks_to_create:
            tracks_to_create.append(track_name)
    for part, track_name in DRUM_PART_TRACK_MAP.items():
        if part in info.get("midi_parts", {}) and track_name not in tracks_to_create:
            tracks_to_create.append(track_name)

    item_actions: list[dict] = [
        {"track": "AI_MASTER", "kind": "audio", "file": info["audio_path"], "start_s": audio_start_s}
    ]
    for stem_key, track_name in STEM_TRACK_MAP.items():
        if stem_key in info["stems"]:
            item_actions.append(
                {"track": track_name, "kind": "audio", "file": info["stems"][stem_key], "start_s": audio_start_s}
            )
    item_actions.append(
        {"track": "MIDI_VOCAL", "kind": "midi", "file": info["vocal_midi_path"], "start_s": audio_start_s}
    )
    item_actions.append(
        {"track": "MIDI_INS", "kind": "midi", "file": info["ins_midi_path"], "start_s": audio_start_s}
    )
    for midi_key, track_name in MIDI_TRACK_MAP.items():
        if midi_key in info["extra_midi"]:
            item_actions.append(
                {
                    "track": track_name,
                    "kind": "midi",
                    "file": info["extra_midi"][midi_key],
                    "start_s": audio_start_s,
                }
            )
    # One item per drum class, each on its own track: kick separate from snare, etc.
    # The combined drums.mid is dropped when parts are present, because inserting both
    # would double every hit.
    for part, track_name in DRUM_PART_TRACK_MAP.items():
        source = info.get("midi_parts", {}).get(part)
        if source:
            item_actions.append(
                {"track": track_name, "kind": "midi", "file": source, "start_s": audio_start_s}
            )
    if info.get("midi_parts"):
        item_actions = [a for a in item_actions if a["track"] != "MIDI_DRUMS"]

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
        "tracks_to_create": tracks_to_create,
        "tracks_to_delete": sorted(set(existing_ai)) if clean else [],
        "regions_to_delete": [
            {"name": m["name"], "start_s": m["start_s"], "index": m["index"]} for m in regions_to_delete
        ],
        "regions_to_create": kept,
        "region_clamps": clamps,
        "region_overhangs": overhangs,
        "region_skipped": skipped,
        "items": [
            {**action, "file": Path(action["file"]).name} for action in item_actions
        ],
        "tempo": info["bpm"],
        "meter": info["meter"],
        "title_marker": info["title"],
        "grid": grid_info,
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
        "items": [],
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
    if not controller.set_tempo(project_bpm):
        warnings.append("set_tempo returned False")
    applied["tempo"] = project_bpm
    if abs(project_bpm - float(info["bpm"])) > 1e-6:
        applied["tempo_note"] = (
            f"project tempo is the MEASURED {project_bpm} bpm, not the score's {info['bpm']} "
            "(the render's own tempo; using the score's would drift the music off the ruler)"
        )
    meter_num, meter_den = info["meter"]
    meter_ok, meter_how = _set_time_signature(project, meter_num, meter_den, project_bpm)
    if not meter_ok:
        warnings.append(f"time signature write returned false ({meter_how})")
    applied["meter"] = info["meter"]
    applied["meter_how"] = meter_how
    RPR.AddProjectMarker(project.id, False, 0.0, 0.0, info["title"], -1)

    # --- tracks (base five, then one per extra artifact the bundle carries)
    track_index: dict[str, int] = {}
    for name in tracks_to_create:
        track_index[name] = controller.create_track(name)
        applied["tracks"].append(name)

    # --- items (audio stems and MIDI performances, all at the arrangement origin)
    # The controller inserts at the edit cursor, and REAPER does not allow a
    # negative cursor - so a negative origin (the render's lead-in) needs the
    # position written afterwards. Measured on the house take: without this the
    # items sat at 0.0 while the regions were shifted, i.e. 0.42 s out of sync.
    applied["items"] = []
    for action in item_actions:
        track = track_index[action["track"]]
        if action["kind"] == "audio":
            outcome = controller.insert_audio_item(track, action["file"], action["start_s"])
        else:
            outcome = controller.insert_midi_item(track, action["file"], action["start_s"])
        if outcome in (-1, None):
            warnings.append(f"{action['kind']} insert failed on {action['track']} ({Path(action['file']).name})")
            continue
        if action["start_s"] != 0.0:
            controller.set_item_position(track, outcome, action["start_s"])
        source_offset = None
        if action["kind"] == "audio" and audio_source_offset_s > 0:
            source_offset = _set_audio_source_offset(project, track, audio_source_offset_s)
            if source_offset is None or abs(source_offset - audio_source_offset_s) > 1e-4:
                warnings.append(
                    f"{action['track']}: take source offset "
                    f"{('unknown' if source_offset is None else f'{source_offset:.3f}')} s does not match "
                    f"the manifest's {audio_source_offset_s:.3f} s"
                )
        position = _last_item_position(project, track)
        if position is None or abs(position - action["start_s"]) > 1e-3:
            warnings.append(
                f"{action['track']}: item position "
                f"{('unknown' if position is None else f'{position:.3f}')} s does not match "
                f"the arrangement origin {action['start_s']:.3f} s"
            )
        if action["kind"] == "audio":
            applied["audio_items"] += 1
        else:
            applied["midi_items"] += 1
        applied["items"].append(
            {
                "track": action["track"],
                "kind": action["kind"],
                "file": Path(action["file"]).name,
                "start_s": action["start_s"],
                "position_s": position,
                "source_offset_s": source_offset,
            }
        )

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
    tracks_ok = all(name in after_names for name in tracks_to_create)
    marks_after = _enumerate_marks(project)
    regions_after = [m for m in marks_after if m["is_region"]]
    # Matching is by time range: marker names are unreadable through reapy
    # (measured - see _enumerate_marks), so a name comparison would always read 0.
    regions_matched = sum(1 for region in kept if any(_same_span(m, region) for m in regions_after))
    missing_items = [
        action["track"]
        for action in item_actions
        if project.tracks[track_index[action["track"]]].n_items < 1
    ]
    items_ok = not missing_items
    if missing_items:
        warnings.append("tracks without the expected item: " + ", ".join(missing_items))
    tempo_after = controller.get_tempo()
    tempo_ok = abs(float(tempo_after) - project_bpm) < 1e-4
    if not tempo_ok:
        warnings.append(f"tempo is {tempo_after}, expected {project_bpm}")
    meter_after = _read_time_signature(project)
    meter_applied_ok = [meter_after[0], meter_after[1]] == info["meter"]

    # --- notes: the check that counting cannot do. A MIDI artifact written at a tempo
    # other than the project's is inserted without complaint and holds the same NUMBER
    # of notes; only their POSITIONS reveal it (measured: an 80.9 s deficit).
    expected_spans = expected_note_spans(info)
    actual_spans: dict[str, dict] = {}
    for key, track_name in MIDI_ARTIFACT_TRACK.items():
        if track_name not in track_index:
            continue
        span = _take_note_span(project, track_index[track_name])
        if span is not None:
            actual_spans[key] = span
    # When the bundle carries per-class parts, the combined drums.mid is deliberately
    # NOT inserted (it would double every hit), so its expectation is dropped rather
    # than reported as unverified.
    superseded: list[str] = []
    if info.get("midi_parts") and "drums" in expected_spans:
        expected_spans.pop("drums")
        superseded.append("drums")
    note_warnings, note_report = compare_note_spans(expected_spans, actual_spans)
    warnings.extend(note_warnings)

    # --- grid phase: how far the notes of each item sit from the project's 1/16 grid.
    # This is the number to look at when an arrangement "almost" lines up: measured on
    # a real bundle, drums sat 6.4 ms and the score-derived parts 21 ms off the grid.
    grid_alignment: dict[str, dict] = {}
    bar_s = float(grid_info.get("bar_s") or 0.0)
    step_s = bar_s / 16.0 if bar_s else 0.0
    if step_s > 0:
        for key, track_name in MIDI_ARTIFACT_TRACK.items():
            if track_name not in track_index:
                continue
            _, deviations = _take_note_phases(project, track_index[track_name], step_s)
            if not deviations:
                continue
            # `phase_is_an_offset` is False when the notes are spread over the whole step:
            # then phase_ms is the median of a flat distribution, not a grid offset (see
            # summarize_note_phase). Those are reported, never warned about as a problem.
            entry = summarize_note_phase(deviations, step_s)
            grid_alignment[key] = entry
            if entry["phase_is_an_offset"] and abs(entry["phase_ms"]) > 10.0:
                warnings.append(
                    f"MIDI '{key}': notes sit {entry['phase_ms']:+.1f} ms off the project grid "
                    f"({entry['share_on_grid']:.0%} of them on it)"
                )
        # THE ANCHOR RULE. The kick carries the placement of the whole drum part: a
        # sampler on it has to trigger on the ruler, and every other class is heard
        # relative to it. So it is held to a stricter standard than the rest - it must
        # actually BE on the grid, not merely near it.
        anchor_key = "drums_kick" if "drums_kick" in grid_alignment else ("drums" if "drums" in grid_alignment else None)
        if anchor_key:
            anchor = grid_alignment[anchor_key]
            anchor["is_anchor"] = True
            on_grid = anchor["share_on_grid"] >= 0.95 and abs(anchor["phase_ms"]) <= 5.0
            anchor["on_grid"] = on_grid
            if not on_grid:
                warnings.append(
                    f"the drum anchor ('{anchor_key}') is NOT cleanly on the grid: "
                    f"phase {anchor['phase_ms']:+.2f} ms, {anchor['share_on_grid']:.0%} of notes on it "
                    "(a sampler on this track would not trigger on the beat)"
                )
        # the audio side: the item plus its source offset must put the first downbeat that
        # the MANIFEST recorded on a bar line of the project. Note what this can and cannot
        # see: it checks that the two placement fields (audio.start_s and
        # audio.source_offset_s) agree with the recorded downbeat - it does NOT re-measure
        # the render, so a wrong downbeat estimate in the manifest would pass here. Where
        # the estimate came from is in grid.origin_refined_by / grid.origin_refine.
        downbeat = audio_start_s + float(grid_info.get("first_downbeat_s") or 0.0) - audio_source_offset_s
        if bar_s > 0:
            delta = downbeat - round(downbeat / bar_s) * bar_s
            grid_alignment["audio"] = {
                "downbeat_s": round(downbeat, 4),
                "bar": round(downbeat / bar_s + 1, 3),
                "delta_ms": round(delta * 1000.0, 2),
                "source_offset_s": round(audio_source_offset_s, 5),
                "recorded_downbeat_s": round(float(grid_info.get("first_downbeat_s") or 0.0), 5),
                "basis": ("placement fields vs the downbeat recorded by the exporter; "
                          "the render itself is not re-measured here"),
            }
            if abs(delta) > 0.005:
                warnings.append(
                    f"audio: the item placement puts the render's RECORDED first downbeat "
                    f"{delta * 1000.0:+.1f} ms off a bar line"
                )
    if superseded:
        note_report["_superseded"] = {
            "artifacts": superseded,
            "reason": "the bundle carries per-class drum parts, which are inserted instead",
        }
    if not expected_spans:
        note_report = {
            "_checked": False,
            "_reason": ("the manifest carries no midi_time block (exported before note verification "
                        "existed), so the note positions were NOT verified"),
        }
        warnings.append(
            "note positions were NOT verified: the manifest carries no midi_time block "
            "(re-export the bundle to get the check)"
        )

    verification = {
        "tracks_ok": tracks_ok,
        "items_ok": items_ok,
        "items_expected": len(item_actions),
        "regions_expected": len(kept),
        "regions_matched": regions_matched,
        "regions_read_back": len(regions_after),
        "region_identity": "start_s/end_s within 0.1 s (reapy returns no marker names)",
        "tempo_after": tempo_after,
        "tempo_expected": project_bpm,
        "tempo_ok": tempo_ok,
        "meter_after": [meter_after[0], meter_after[1]],
        "meter_ok": meter_applied_ok,
        "midi_notes": note_report,
        "grid_alignment": grid_alignment,
        "audio_start_s": audio_start_s,
        "grid": grid_info,
    }
    # Bookkeeping entries (`_checked`, `_superseded`) are not measurements - see
    # note_report_ok. `checked: False` inside a real entry still fails.
    notes_ok = note_report_ok(note_report)
    notes_checked = bool(expected_spans)
    # The two grid guarantees are things THIS apply controls (the anchor is snapped and the
    # audio item is placed on the bar line by construction), so a miss there is a real
    # shortfall. The other artifacts' phases are MEASUREMENTS of a loose performance and
    # are reported, not judged.
    anchor_on_grid = (
        True if (step_s <= 0 or anchor_key is None)
        else bool(grid_alignment.get(anchor_key, {}).get("on_grid"))
    )
    audio_delta_ok = (
        True if step_s <= 0
        else abs(float(grid_alignment.get("audio", {}).get("delta_ms") or 0.0)) <= 5.0
    )
    grid_ok = anchor_on_grid and audio_delta_ok
    verification["grid_ok"] = grid_ok
    verification["anchor_on_grid"] = anchor_on_grid
    verification["audio_on_bar_line"] = audio_delta_ok
    verification["notes_checked"] = notes_checked
    ok = bool(
        tracks_ok and items_ok and regions_matched == len(kept) and meter_applied_ok
        and notes_ok and tempo_ok and grid_ok
    )
    if not ok:
        warnings.append(
            f"verification shortfall: tracks_ok={tracks_ok}, items_ok={items_ok}, "
            f"regions {regions_matched}/{len(kept)}, meter {verification['meter_after']}, "
            f"notes_ok={notes_ok}, grid_ok={grid_ok} "
            f"(anchor '{anchor_key}' on grid: {anchor_on_grid}; audio on a bar line: {audio_delta_ok})"
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
        "region_overhangs": overhangs,
        "regions_skipped": skipped,
        "stems": {key: Path(value).name for key, value in info["stems"].items()},
        "midi_sources": info["midi_sources"],
        "midi_time": expected_spans,
        "grid": grid_info,
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
        "region_overhangs": overhangs,
        "regions_skipped": skipped,
        "verification": verification,
        "warnings": warnings,
        "apply_report_path": str(report_path),
    }
