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

import contextlib
import json
import time
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


def _set_item_slice(project, track_index: int, *, length_s: float, source_offset_s: float) -> dict:
    """Make the newest item on a track play exactly one SLICE of its source.

    This is what turns one MIDI file into per-region items: the item keeps the region's own span
    (in project seconds, so its edges land on the bar lines the ruler draws) and looks at the
    source from `source_offset_s` - the same relation REAPER creates when a user splits an item.
    Both values are READ BACK, because a write that is ignored must not read as a success:
    measured on item positions, `SetMediaItemInfo_Value(item, 'D_POSITION', -0.42)` returns 1 and
    changes nothing.
    """
    track = project.tracks[track_index]
    count = int(RPR.CountTrackMediaItems(track.id))
    if count < 1:
        return {"source_offset_s": None, "length_s": None, "reason": "no items"}
    item = RPR.GetTrackMediaItem(track.id, count - 1)
    take = RPR.GetActiveTake(item)
    if not take:
        return {"source_offset_s": None, "length_s": None, "reason": "no take"}
    RPR.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", float(source_offset_s))
    RPR.SetMediaItemInfo_Value(item, "D_LENGTH", float(length_s))
    return {
        "source_offset_s": float(RPR.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS")),
        "length_s": float(RPR.GetMediaItemInfo_Value(item, "D_LENGTH")),
    }


def cut_actions_by_segments(
    item_actions: list[dict], segments: dict[str, list[dict]] | None, *, origin_s: float
) -> list[dict]:
    """Replace each MIDI action that HAS segments with one action per segment (pure, testable).

    The user's own prescription ("insert the MIDI in pieces under the regions, not as one
    continuous signal"), and the manifest carries what it needs: `midi_segments` holds, per artifact,
    the runs the normalizer measured in the AUDIO - one row per region that holds notes, spanning the
    REGION's own bar-line boundaries. Each row becomes its own item: start at the arrangement origin
    PLUS the row's start, length = the row's span, and the take looks at the source from that same
    start (REAPER's own split relation). A part that plays in half the sections therefore sits on the
    ruler as the pieces it actually plays, and the regions between them hold no item at all.
    """
    if not segments:
        return list(item_actions)
    sliced: list[dict] = []
    for action in item_actions:
        rows = segments.get(action.get("artifact")) if action.get("kind") == "midi" else None
        if not rows:
            sliced.append(action)
            continue
        for index, row in enumerate(rows):
            start = float(row["start_s"])
            length = float(row["end_s"]) - start
            sliced.append(
                {
                    **action,
                    "start_s": float(origin_s) + start,
                    "length_s": length,
                    "source_offset_s": start,
                    "segment": index,
                    "segment_name": row.get("name") or "",
                    "segment_notes": int(row.get("notes") or 0),
                }
            )
    return sliced


def _same_span(mark: dict, region: dict) -> bool:
    """True when a project mark covers the manifest region's range (both ends)."""
    return (
        abs(mark["start_s"] - region["start_s"]) <= _MATCH_TOLERANCE_S
        and abs(mark["end_s"] - region["end_s"]) <= _MATCH_TOLERANCE_S
    )


def _note_time_map(take, low_ppq: int, high_ppq: int, *, probes: int = 8) -> tuple[object, dict]:
    """A calibrated PPQ -> project-seconds map, PROVEN on samples it did not use.

    MEASURED, and this is the whole reason the function exists: one bridge call costs ~32 ms
    (the reapy bridge is a JSON socket), and the old loop asked the bridge for every note AND
    for every note's project time - 62.89 ms per note, measured on a live project. A fill with
    4100 notes therefore spent ~260 s in verification alone, and the fill took 300 s.

    A take's PPQ -> seconds map is affine when the tempo map does not change inside the item,
    which is the normal case. `low`/`high` measure the line; `probes` further positions CHECK
    it, and if any deviates the map is refused (`exact: False`) so the caller can fall back to
    asking per note. The check is not decoration: without it, a tempo marker inside the item
    would silently give every note a wrong time, which is exactly the defect the check exists
    to find.
    """
    if high_ppq <= low_ppq:
        return None, {"exact": False, "reason": "no PPQ span to interpolate"}
    started = time.perf_counter()
    low_s = float(RPR.MIDI_GetProjTimeFromPPQPos(take, float(low_ppq)))
    high_s = float(RPR.MIDI_GetProjTimeFromPPQPos(take, float(high_ppq)))
    seconds_per_tick = (high_s - low_s) / float(high_ppq - low_ppq)
    worst_ms = 0.0
    for step in range(1, probes + 1):
        probe = low_ppq + (high_ppq - low_ppq) * step // (probes + 1)
        if probe <= low_ppq or probe >= high_ppq:
            continue
        measured = float(RPR.MIDI_GetProjTimeFromPPQPos(take, float(probe)))
        predicted = low_s + (probe - low_ppq) * seconds_per_tick
        worst_ms = max(worst_ms, abs(measured - predicted) * 1000.0)
    report = {
        "exact": worst_ms <= 0.05,
        "probes": probes,
        "worst_probe_ms": round(worst_ms, 6),
        "calls": probes + 2,
        "seconds": round(time.perf_counter() - started, 3),
    }
    if not report["exact"]:
        report["reason"] = (
            "the PPQ -> seconds map is NOT affine inside this item (a tempo/meter change): "
            "every note time is asked for individually instead"
        )

    def resolve(ppq: int) -> float:
        return low_s + (ppq - low_ppq) * seconds_per_tick

    return resolve, report


def notes_in_item_window(times: list[float], position_s: float, length_s: float) -> list[float]:
    """The notes of a take that SOUND in the item, given their project times.

    MEASURED reason this exists (afro take, 2026-10-18): a take's note list is the SOURCE's, whatever
    the item's source offset is, so a part cut into 3 slices read 288 notes against the file's 96 -
    exactly 3x, and the fill refused the project as unverified. The notes that sound are the ones
    whose project time falls inside the item, and the mapping already accounts for the source offset
    (measured on the bass: a slice starting at project 78.689 reports its notes from 78.689, not from
    the item's start). Filtering by the window is therefore what makes the union over a track's items
    count each note exactly once.
    """
    start = float(position_s)
    end = start + float(length_s)
    return [when for when in times if start - 1e-6 <= when <= end + 1e-6]


def _take_note_times(project, track_index: int) -> dict:
    """Every note time of EVERY item on a track, in one pass each.

    Returns {"notes", "times", "time_map", "items"} - the PPQ of each note comes from
    `MIDI_GetNote` (unavoidable: reapy cannot return `MIDI_GetAllEvts`' buffer, measured -
    the call succeeds with bufSize 6964 and hands back an EMPTY string, the same limitation
    that makes region names unreadable), while the PPQ -> seconds map is calibrated once per
    item instead of once per note.

    WHY EVERY ITEM AND NOT ONLY THE NEWEST (measured reason, 2026-10-18): the fill inserts ONE
    ITEM PER REGION when the manifest carries `midi_segments` (the user's "cut it by regions,
    not one continuous signal"), so the newest item holds ONE region's notes while the track
    holds the part. Reading the newest alone would report a part as too short - and its phase
    as whatever that one region happened to hold. The aggregation is exact because every time
    comes back in PROJECT seconds (`MIDI_GetProjTimeFromPPQPos`), which already accounts for
    the item's position AND its source offset.
    """
    track = project.tracks[track_index]
    count = int(RPR.CountTrackMediaItems(track.id))
    if count < 1:
        return {"notes": 0, "times": [], "time_map": None, "items": 0, "reason": "no items"}
    times: list[float] = []
    total_notes = 0
    map_report: dict | None = None
    read_items = 0
    for item_index in range(count):
        item = RPR.GetTrackMediaItem(track.id, item_index)
        take = RPR.GetActiveTake(item)
        if not take:
            continue
        counted = RPR.MIDI_CountEvts(take, 0, 0, 0)
        if not isinstance(counted, (list, tuple)) or len(counted) < 3:
            continue
        notes = int(counted[2])
        if notes < 1:
            continue
        ppqs: list[int] = []
        for note_index in range(notes):
            got = RPR.MIDI_GetNote(take, note_index, 0, 0, 0, 0, 0, 0, 0)
            if not isinstance(got, (list, tuple)) or len(got) < 6:
                continue
            ppqs.append(int(float(got[5])))
        if not ppqs:
            total_notes += notes
            continue
        read_items += 1
        resolve, report = _note_time_map(take, min(ppqs), max(ppqs))
        if resolve is None or not report.get("exact"):
            # honest fallback: the calibrated map was refused, so ask per note (the slow path)
            item_times = [float(RPR.MIDI_GetProjTimeFromPPQPos(take, float(ppq))) for ppq in ppqs]
            report = dict(report or {}, used="per_note", calls=len(ppqs))
        else:
            item_times = [resolve(ppq) for ppq in ppqs]
            report = dict(report, used="calibrated", notes=notes)
        # A SLICED ITEM HOLDS THE WHOLE FILE, but plays only its own window: the take's note list is
        # the source's, whatever the item's source offset is (measured: a 3-slice cymbal part read
        # 288 notes against the file's 96 - exactly 3x). The notes that SOUND in this item are the
        # ones whose project time falls inside it, and the mapping already accounts for the source
        # offset (measured on the bass: a slice starting at project 78.689 reports its notes from
        # 78.689, not from the item start). Filtering by the window is therefore what makes the union
        # over the track count each note exactly once.
        position_s = float(RPR.GetMediaItemInfo_Value(item, "D_POSITION"))
        length_s = float(RPR.GetMediaItemInfo_Value(item, "D_LENGTH"))
        inside = notes_in_item_window(item_times, position_s, length_s)
        times.extend(inside)
        total_notes += len(inside) if len(inside) != len(item_times) else notes
        report["items"] = count
        report["sounding_notes"] = len(inside)
        if map_report is None:
            map_report = report
    if not times:
        return {
            "notes": total_notes,
            "times": [],
            "time_map": map_report,
            "items": count,
            "reason": "no readable notes",
        }
    return {"notes": total_notes, "times": times, "time_map": map_report, "items": read_items}


def _take_note_phases(
    project, track_index: int, step_s: float, cache: dict | None = None
) -> tuple[dict, list[float]]:
    """The newest item's note span AND how far its notes sit from the project grid.

    Returns (span, deviations) where a deviation is the distance from the nearest
    multiple of `step_s`, in [-step/2, +step/2]. The median of those deviations is the
    whole item's phase: measured on a real bundle, the drum part sat 6.4 ms off the
    1/16 grid and the score-derived parts 21 ms - the reason an arrangement could look
    "almost right but not on the grid".

    `cache` shares ONE reading of an item between the span pass and the grid pass: the
    verification used to read every note twice (measured: 62.89 ms per note over the bridge,
    so 4100 notes cost ~260 s of a 300 s fill).

    Times come from the PPQ->seconds map (see `_note_time_map`), which is what makes this
    check able to see a tempo defect at all: it converts through the PROJECT tempo, so a file
    written at 120 bpm inside a 172 bpm project reports its notes 1.43x too early.
    """
    if cache is not None and track_index in cache:
        reading = cache[track_index]
    else:
        reading = _take_note_times(project, track_index)
        if cache is not None:
            cache[track_index] = reading
    times = list(reading["times"])
    if not times:
        return {"notes": reading["notes"], "first_s": None, "last_s": None}, []
    span = {"notes": int(reading["notes"]), "first_s": min(times), "last_s": max(times)}
    if step_s <= 0:
        return span, []
    deviations = [((t % step_s) + step_s / 2.0) % step_s - step_s / 2.0 for t in times]
    return span, deviations


def _take_note_span(project, track_index: int, cache: dict | None = None) -> dict | None:
    span, _ = _take_note_phases(project, track_index, 0.0, cache)
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


class _PhaseTimer:
    """Seconds per phase of a fill, because "why is this slow?" was asked about the FILL.

    Measured context: one bridge call costs ~32 ms (reapy is a JSON socket), so the phases that
    touch every note item dominate everything else, and a phase that is quietly quadratic looks
    like "REAPER is slow". The numbers land in the apply report beside the verification.
    """

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    @contextlib.contextmanager
    def phase(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] = round(self.seconds.get(name, 0.0) + time.perf_counter() - started, 2)

    def counted(self, name: str, by: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + by

    def report(self) -> dict:
        # `seconds_total` sums only the TOP-LEVEL phases: a sub-phase (items:audio) runs INSIDE
        # its parent, so adding every entry would count the items twice and report a total larger
        # than the fill took. Per-phase numbers stay as measured either way.
        top_level = {name: seconds for name, seconds in self.seconds.items() if ":" not in name}
        return {
            "seconds_by_phase": dict(sorted(self.seconds.items(), key=lambda item: -item[1])),
            "counts": dict(sorted(self.counts.items())),
            "seconds_total": round(sum(top_level.values()), 2),
            "seconds_total_note": "top-level phases only; a colon in a name means a sub-phase inside one",
        }


def _load_warp_markers(bundle: Path) -> tuple[list[dict], dict]:
    """The warp plan of a bundle, written by mcp-audiocpp :: plan_warp as warp.json.

    Why the plan is a FILE and not in the manifest: it can be recomputed without touching the
    bundle, and a bundle that is already applied to a live project cannot be rewritten at all
    (measured: REAPER holds song.wav open, so a re-export over it fails).
    """
    path = bundle / "warp.json"
    if not path.is_file():
        return [], {"present": False, "reason": "the bundle carries no warp.json"}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], {"present": True, "markers": 0, "reason": f"warp.json is unreadable: {exc}"}
    markers = [marker for marker in (document.get("markers") or []) if isinstance(marker, dict)]
    return markers, {
        "present": True,
        "markers": len(markers),
        "mode": document.get("mode"),
        "source": document.get("source"),
        "window_bars": document.get("window_bars"),
        "hop_bars": document.get("hop_bars"),
        "offset_ms": document.get("offset_ms"),
        "residual_before_ms": document.get("residual_before_ms"),
        "residual_after_warp_ms": document.get("residual_after_warp_ms"),
        "worst_local_stretch": document.get("worst_local_stretch"),
    }


def _apply_stretch_markers(
    project, track_index: int, markers: list[dict], *, source_end_s: float | None = None
) -> dict:
    """Set a warp plan's stretch markers on a track's newest item, then READ THEM BACK.

    Measured (F42): a render's timing drifts - the kick element's attack sits -1.2 ms from the
    1/16 lines in bars 35-42 and +10.2 ms in bars 179-186 (about 56 ppm) - while the transcribed
    notes stay at +0.08 ms of the same lines. A constant item position cannot follow a drift, so
    the audio itself is warped: each marker says "source second S sounds at project second P".

    Three measured details this function exists for, each of which produced a wrong project:

      * the list is CLEARED first. `DeleteTakeStretchMarkers(take, 0, 1000)` is the shape that
        works (the singular `DeleteTakeStretchMarker` does not exist in reapy), and adding onto a
        list that is already there is what made the first attempt's second half play at 0.7x;
      * the item's LENGTH is written explicitly. Measured: after the markers were set the item
        kept length 320.78 s for 269.87 s of audio, and nothing recomputed it - the end marker sat
        at 320.78, so the last 16 s of source would play at 4x. Writing D_LENGTH fixes it and the
        value sticks, while clearing the markers does NOT (the stale length is independent);
      * the read-back compares EVERY marker, matched by INDEX. `GetTakeStretchMarker` returns
        `[retval, take, idx, pos, srcpos]` through reapy, where retval is the marker's index and
        -1 past the end - reading `got[0]` as a boolean makes marker 0 look like "not found",
        which is how a fill reported "0 of 37 read back" while all 37 were in the project.
    """
    track = project.tracks[track_index]
    count = int(RPR.CountTrackMediaItems(track.id))
    if count < 1:
        return {"markers": 0, "read_back": 0, "reason": "no item on the track"}
    item = RPR.GetTrackMediaItem(track.id, count - 1)
    take = RPR.GetActiveTake(item)
    if not take:
        return {"markers": 0, "read_back": 0, "reason": "the item has no take"}

    RPR.DeleteTakeStretchMarkers(take, 0, 1000)
    accepted = 0
    refused: list[int] = []
    for index, marker in enumerate(markers):
        try:
            pos = float(marker.get("project_s"))
            source = float(marker.get("source_s"))
        except (TypeError, ValueError):
            refused.append(index)
            continue
        outcome = RPR.SetTakeStretchMarker(take, -1, pos, source)
        if outcome in (-1, None, False, 0):
            refused.append(index)
        else:
            accepted += 1

    length_written = None
    if source_end_s is not None and markers:
        last = max(markers, key=lambda mark: float(mark.get("source_s") or 0.0))
        # beyond the last marker the take plays at the item's own rate, so the sounding end is
        # the last anchor plus the remaining source
        length_written = float(last["project_s"]) + (float(source_end_s) - float(last["source_s"]))
        RPR.SetMediaItemInfo_Value(item, "D_LENGTH", length_written)

    stored: list[tuple[float, float]] = []
    for probe in range(len(markers) + 16):
        raw = RPR.GetTakeStretchMarker(take, probe, 0, 0)
        if not isinstance(raw, (list, tuple)) or len(raw) < 5 or int(raw[0]) < 0:
            break
        stored.append((float(raw[3]), float(raw[4])))
    worst_ms = 0.0
    for index, marker in enumerate(markers):
        if index >= len(stored):
            break
        worst_ms = max(
            worst_ms,
            abs(stored[index][0] - float(marker["project_s"])) * 1000.0,
            abs(stored[index][1] - float(marker["source_s"])) * 1000.0,
        )
    return {
        "markers": accepted,
        "read_back": len(stored),
        "refused": refused[:8],
        "worst_position_ms": round(worst_ms, 4),
        "length_s": round(length_written, 4) if length_written else None,
        "reason": None if accepted == len(markers) else "REAPER refused some markers",
    }


def apply_bundle(
    bundle_dir: str | None,
    *,
    controller,
    dry_run: bool = False,
    clean: bool = False,
    save_as: str | None = None,
    apply_warp: bool = True,
    project_id: str | None = None,
) -> dict:
    """Validate, (optionally preview), mutate, verify, report.

    Raises ManifestError for anything that must refuse before touching the host.
    Returns the report dict (ok is False when verification finds less than the
    requested count applied, even though the mutations ran).

    `project_id` pins WHICH project is filled, given as the `(ReaProject*)0x...`
    string `new_project_tab` returns. Without it the fill uses whatever project is
    CURRENT - which is a race, and it was measured on a live session (2026-10-16):
    the tab tool opened an empty tab and read it back as current (track_count 0),
    and the fill 2.7 s later found the PREVIOUS project's 15 AI_* tracks and refused
    the second apply. Two calls that must act on the same project cannot depend on a
    third party not changing the selection between them.

    `apply_warp` puts the bundle's warp.json (mcp-audiocpp :: plan_warp) onto every audio item as
    stretch markers. It is on by default because the plan only exists when somebody deliberately
    measured it, and off is available for a caller that wants the audio untouched.
    """
    bundle = resolve_bundle_dir(bundle_dir)
    info = load_manifest(bundle)
    warp_markers, warp_info = _load_warp_markers(bundle) if apply_warp else ([], {"present": False, "reason": "warp was switched off"})

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
    for track_name, path_key in (("MIDI_VOCAL", "vocal_midi_path"), ("MIDI_INS", "ins_midi_path")):
        if not info.get(path_key) and track_name in tracks_to_create:
            tracks_to_create.remove(track_name)
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
    # The two SCORE-derived MIDI artifacts are optional: a manifest whose score could not be read
    # carries no `midi.vocal` / `midi.ins`, and then there is no item to place - and no empty track to
    # leave behind either, which is why the names come OUT of the track list below instead of being
    # created empty.
    if info["vocal_midi_path"]:
        item_actions.append(
            {
                "track": "MIDI_VOCAL",
                "kind": "midi",
                "file": info["vocal_midi_path"],
                "start_s": audio_start_s,
                "artifact": "vocal",
            }
        )
    if info["ins_midi_path"]:
        item_actions.append(
            {
                "track": "MIDI_INS",
                "kind": "midi",
                "file": info["ins_midi_path"],
                "start_s": audio_start_s,
                "artifact": "ins",
            }
        )
    for midi_key, track_name in MIDI_TRACK_MAP.items():
        if midi_key in info["extra_midi"]:
            item_actions.append(
                {
                    "track": track_name,
                    "kind": "midi",
                    "file": info["extra_midi"][midi_key],
                    "start_s": audio_start_s,
                    "artifact": midi_key,
                }
            )
    # One item per drum class, each on its own track: kick separate from snare, etc.
    # The combined drums.mid is dropped when parts are present, because inserting both
    # would double every hit.
    for part, track_name in DRUM_PART_TRACK_MAP.items():
        source = info.get("midi_parts", {}).get(part)
        if source:
            item_actions.append(
                {
                    "track": track_name,
                    "kind": "midi",
                    "file": source,
                    "start_s": audio_start_s,
                    "artifact": f"drums_{part}",
                }
            )
    if info.get("midi_parts"):
        item_actions = [a for a in item_actions if a["track"] != "MIDI_DRUMS"]

    # ---- CUT BY REGION: one item per segment, not one item per file (see
    # cut_actions_by_segments for the rule and where the rows come from).
    item_actions = cut_actions_by_segments(item_actions, info.get("midi_segments"), origin_s=audio_start_s)

    project = reapy.Project(project_id) if project_id else reapy.Project()
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
        "warp": {**warp_info, "first_marker": warp_markers[0] if warp_markers else None},
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
        "warp": [],
    }

    # --- clean (regions first: their recorded indices come from the pre-mutation state)
    phases = _PhaseTimer()
    with phases.phase("clean"):
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
    with phases.phase("tracks"):
        for name in tracks_to_create:
            track_index[name] = controller.create_track(name)
            applied["tracks"].append(name)
            phases.counted("tracks")

    # --- items (audio stems and MIDI performances, all at the arrangement origin)
    # The controller inserts at the edit cursor, and REAPER does not allow a
    # negative cursor - so a negative origin (the render's lead-in) needs the
    # position written afterwards. Measured on the house take: without this the
    # items sat at 0.0 while the regions were shifted, i.e. 0.42 s out of sync.
    applied["items"] = []
    with phases.phase("items"):
        for action in item_actions:
            phases.counted("items")
            with phases.phase("items:" + action["kind"]):
                track = track_index[action["track"]]
                if action["kind"] == "audio":
                    outcome = controller.insert_audio_item(track, action["file"], action["start_s"])
                else:
                    outcome = controller.insert_midi_item(track, action["file"], action["start_s"])
            if outcome in (-1, None):
                warnings.append(
                    f"{action['kind']} insert failed on {action['track']} ({Path(action['file']).name})"
                )
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
            if action["kind"] == "midi" and action.get("source_offset_s") is not None:
                # A SLICED ITEM (see the 'cut by region' block): its length and the part of the file
                # it reads are both written and READ BACK, because a take that ignores the write
                # would otherwise be reported as placed - measured on item positions, a refused
                # value returns success.
                slice_report = _set_item_slice(
                    project,
                    track,
                    length_s=float(action["length_s"]),
                    source_offset_s=float(action["source_offset_s"]),
                )
                source_offset = slice_report.get("source_offset_s")
                if source_offset is None or abs(source_offset - action["source_offset_s"]) > 1e-3:
                    warnings.append(
                        f"{action['track']} ({Path(action['file']).name}): the slice's source offset "
                        f"{('is unknown' if source_offset is None else f'is {source_offset:.3f} s')} "
                        f"but the manifest says {action['source_offset_s']:.3f} s"
                    )
                placed_length = slice_report.get("length_s")
                if placed_length is None or abs(placed_length - action["length_s"]) > 1e-3:
                    warnings.append(
                        f"{action['track']} ({Path(action['file']).name}): the slice's length "
                        f"{('is unknown' if placed_length is None else f'is {placed_length:.3f} s')} "
                        f"but the manifest says {action['length_s']:.3f} s"
                    )
            position = _last_item_position(project, track)
            if position is None or abs(position - action["start_s"]) > 1e-3:
                warnings.append(
                    f"{action['track']}: item position "
                    f"{('unknown' if position is None else f'{position:.3f}')} s does not match "
                    f"the arrangement origin {action['start_s']:.3f} s"
                )
            warp_result = None
            if action["kind"] == "audio" and warp_markers:
                # Every audio item carries the SAME markers: the mix and its stems share one
                # timeline, so the deviation measured on any of them is the deviation of all.
                with phases.phase("warp"):
                    warp_result = _apply_stretch_markers(
                        project, track, warp_markers, source_end_s=info["duration_s"]
                    )
                phases.counted("warp")
                applied["warp"].append({"track": action["track"], **warp_result})
                if warp_result.get("read_back", 0) < len(warp_markers):
                    warnings.append(
                        f"{action['track']}: the take holds {warp_result.get('read_back')} of the "
                        f"{len(warp_markers)} planned stretch markers - the audio is NOT fully warped "
                        f"({warp_result.get('reason') or 'unstated'})"
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
                    # A sliced item carries which region it IS (see the 'cut by region' block):
                    # without it the report of a 5-item part looks like five unrelated inserts.
                    "segment": action.get("segment"),
                    "segment_name": action.get("segment_name"),
                    "segment_notes": action.get("segment_notes"),
                    "length_s": action.get("length_s"),
                }
            )

    # --- regions (validated AddProjectMarker path)
    with phases.phase("regions"):
        for region in kept:
            RPR.AddProjectMarker(project.id, True, region["start_s"], region["end_s"], region["name"], -1)
            applied["regions"] += 1
            phases.counted("regions")

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
    # Timed with a stamp rather than a `with`: the block runs to the end of the function and
    # wrapping it would mean re-indenting every line of it (a large diff for a measurement).
    verify_started = time.perf_counter()
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
    # ONE reading per item, shared by the span pass and the grid pass below. Reading them
    # twice is what made the fill slow (measured: 62.89 ms per note over the reapy bridge).
    note_cache: dict[int, dict] = {}
    for key, track_name in MIDI_ARTIFACT_TRACK.items():
        if track_name not in track_index:
            continue
        span = _take_note_span(project, track_index[track_name], note_cache)
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
    # The PPQ -> seconds maps the note times came from, so a reader can see HOW the times
    # were obtained: `calibrated` (a few calls, proven on probes) or `per_note` (the slow
    # path, taken when the map was refused). A silent choice here would hide a two-minute
    # difference, and more importantly it would hide a tempo change inside an item.
    note_time_maps: dict[str, dict] = {}
    for key, track_name in MIDI_ARTIFACT_TRACK.items():
        index = track_index.get(track_name)
        reading = note_cache.get(index) if index is not None else None
        if reading and reading.get("time_map"):
            note_time_maps[key] = reading["time_map"]
    if step_s > 0:
        for key, track_name in MIDI_ARTIFACT_TRACK.items():
            if track_name not in track_index:
                continue
            _, deviations = _take_note_phases(project, track_index[track_name], step_s, note_cache)
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
        "note_time_maps": note_time_maps,
        "audio_start_s": audio_start_s,
        "grid": grid_info,
        "warp": {
            **warp_info,
            "applied": applied["warp"],
            "read_back_total": sum(entry.get("read_back", 0) for entry in applied["warp"]),
            "audio_tracks": len(applied["warp"]),
            "expected_per_track": len(warp_markers),
        },
    }
    # Where the fill's own time went, measured rather than guessed: one bridge call is ~32 ms and
    # the phase that touches every item dominates, which is exactly the kind of thing that gets
    # blamed on "REAPER being slow".
    phases.seconds["verify"] = round(
        phases.seconds.get("verify", 0.0) + time.perf_counter() - verify_started, 2
    )
    verification["phases"] = phases.report()
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
