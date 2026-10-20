"""audiocpp manifest v1 -> `Project` (F3).

The input is the dict `reaper_reapy_mcp.manifest.manifest.load_manifest()` returns,
passed in as PLAIN DATA: this package imports nothing from `reaper_reapy_mcp`, so
it stays movable and host-free (ARCHITECTURE section 4). The caller loads and
validates the bundle; this module only decides what the project looks like.

Every rule below was measured against the live fill's own output
(`aligned_v4/project/dnb-neurofunk-174.rpp`) or against REAPER itself; the numbers
in the comments are the checks from `tests/rpp_builder/test_audiocpp_compile.py`.

Track and item order mirror `manifest/apply.py` exactly, because `IID` is the
item's number in the project in INSERTION order (1-6 are the six audio items, then
the MIDI items), not its place on the track.

The audio item's LENGTH is the warp's own end: `last.project_s + (duration -
last.source_s)` - the same expression `_apply_stretch_markers` writes, measured to
reproduce the reference's 211.873893 s from its 145-marker warp plan.

A MIDI item that is NOT sliced gets the length REAPER gives an inserted file:
the file's last tick rounded UP to whole beats, converted at the PROJECT tempo.
Measured live: ins.mid (295322 ticks) -> 616 beats, drums.mid -> 612, vocal.mid ->
597 (not 600, which is why the rounding is beats and not bars).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..model.item import AudioSource, Item, MidiEvent, MidiSource, StretchMarker
from ..model.midi_file import MidiFile, MidiFileEvent, read_midi_file, take_name
from ..model.project import Marker, Project, Region
from ..model.track import Track

#: Track order the live fill creates (manifest/apply.py: BASE_AI_TRACKS, then
#: STEM_TRACK_MAP, then MIDI_TRACK_MAP, then DRUM_PART_TRACK_MAP).
BASE_AI_TRACKS: tuple[str, ...] = (
    "AI_MASTER",
    "AI_VOCAL",
    "AI_INSTR",
    "MIDI_VOCAL",
    "MIDI_INS",
)
STEM_TRACK_MAP: tuple[tuple[str, str], ...] = (
    ("vocals", "AI_VOCAL"),
    ("instrumental", "AI_INSTR"),
    ("drums", "AI_DRUMS"),
    ("bass", "AI_BASS"),
    ("other", "AI_OTHER"),
)
MIDI_TRACK_MAP: tuple[tuple[str, str], ...] = (("bass", "MIDI_BASS"),)
DRUM_PART_TRACK_MAP: tuple[tuple[str, str], ...] = (
    ("kick", "MIDI_DRUMS_KICK"),
    ("snare", "MIDI_DRUMS_SNARE"),
    ("hihat", "MIDI_DRUMS_HIHAT"),
    ("tom", "MIDI_DRUMS_TOM"),
    ("cymbal", "MIDI_DRUMS_CYMBAL"),
)

#: The tolerance `_trim_item_to_slice` uses when deciding which slice a note
#: belongs to: a note exactly on an edge belongs to the NEXT slice.
_TRIM_EPS_S = 1e-4


def _ticks_to_seconds(tick: int, ppq: int, bpm: float) -> float:
    return float(tick) / float(ppq) * 60.0 / float(bpm)


def _pair_notes(midi: MidiFile) -> list[tuple[int, int]]:
    """Pair each note-off with the MOST RECENT open note of that pitch (LIFO).

    MEASURED, and it is not what a first reading suggests: MIDI says nothing
    about overlapping notes of the same pitch, and the aligned_v4 vocal file
    holds many (e.g. on 66360 and 70440 with offs at 70453 and 72352). Pairing
    FIFO kept off 71401 and dropped 72352; the reference REPLACES them - it keeps
    (66360, 72352) - which is the stack order. All six previously differing
    items agreed with LIFO and none with FIFO.

    An unmatched note-on is dropped: a note the file never ends is not a note
    REAPER would keep either, and inventing an end would put an event in the
    project that the source does not hold.
    """
    pending: dict[tuple[int, int], list[int]] = {}
    pairs: list[tuple[int, int]] = []
    for index, event in enumerate(midi.events):
        key = (event.status & 0x0F, event.data1)
        if event.is_note_on:
            pending.setdefault(key, []).append(index)
        elif event.is_note_off:
            waiting = pending.get(key)
            if waiting:
                pairs.append((waiting.pop(), index))
    return pairs


def _events_for_window(
    midi: MidiFile,
    bpm: float,
    start_s: float | None,
    end_s: float | None,
) -> tuple[MidiEvent, ...]:
    """The channel events of a slice, in tick order.

    WHICH NOTES A SLICE OWNED is `_trim_item_to_slice`'s rule: the note's START
    is compared with the window `[start - eps, end - eps)`. Its note-off travels
    with it even when the off lies past the end - that is what deleting a note in
    REAPER does, and the reference's last vocal item holds 309 `E` lines for 154
    notes for exactly that reason.

    A non-note channel event (CC, aftertouch, pitch bend) has no note to belong
    to, so its own tick decides; the bundle's files carry none, and this keeps a
    future file from losing them silently.
    """
    pairs = _pair_notes(midi)
    paired: set[int] = set()
    kept_indices: set[int] = set()
    for on_index, off_index in pairs:
        paired.add(on_index)
        paired.add(off_index)
        when = _ticks_to_seconds(midi.events[on_index].tick, midi.ppq, bpm)
        if start_s is not None and when < start_s - _TRIM_EPS_S:
            continue
        if end_s is not None and when >= end_s - _TRIM_EPS_S:
            continue
        kept_indices.add(on_index)
        kept_indices.add(off_index)  # the off travels with its note
    for index, event in enumerate(midi.events):
        if index in paired or event.kind in (0x80, 0x90):
            continue  # notes are decided above, with their partner
        when = _ticks_to_seconds(event.tick, midi.ppq, bpm)
        if start_s is not None and when < start_s - _TRIM_EPS_S:
            continue
        if end_s is not None and when >= end_s - _TRIM_EPS_S:
            continue
        kept_indices.add(index)
    # ORDER AT EQUAL TICKS: note-offs first, then note-ons, each group in file
    # order. MEASURED on the reference: 145 multi-event ticks, 145 of them
    # list every note-off ahead of every note-on and none does otherwise - and
    # in one of them the source file holds the ON first, so REAPER REORDERS
    # rather than copying. A stable sort keeps everything else as it was.
    kept = [
        MidiEvent(event.tick, event.status, event.data1, event.data2)
        for index, event in enumerate(midi.events)
        if index in kept_indices
    ]
    kept.sort(key=lambda item: (item.tick, 0 if item.status & 0xF0 in (0x80,) or (item.status & 0xF0) == 0x90 and item.data2 == 0 else 1))
    return tuple(kept)


def _load_warp(bundle: Path) -> tuple[list[dict], dict]:
    """The bundle's warp plan (mcp-audiocpp :: plan_warp), or no markers."""
    path = bundle / "warp.json"
    if not path.is_file():
        return [], {"present": False, "reason": "the bundle carries no warp.json"}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001 - a bad plan is reported, not raised
        return [], {"present": True, "markers": 0, "reason": "warp.json is unreadable: %s" % exc}
    markers = [marker for marker in (document.get("markers") or []) if isinstance(marker, dict)]
    return markers, {"present": True, "markers": len(markers), "mode": document.get("mode")}


def _audio_length(
    duration_s: float, source_offset_s: float, markers: list[StretchMarker]
) -> float:
    """The item length the warp produces (see the module docstring).

    Without a warp plan the item is the file minus the skipped lead-in: the same
    `duration - source_offset` the live fill would end up with.
    """
    if markers:
        last = max(markers, key=lambda marker: marker.source_pos_s)
        return last.project_pos_s + (duration_s - last.source_pos_s)
    return duration_s - source_offset_s


def _midi_whole_length(midi: MidiFile, bpm: float) -> float:
    """Length REAPER gives an inserted MIDI file: last tick rounded UP to whole beats."""
    beats = float(midi.end_tick) / float(midi.ppq)
    return math.ceil(beats) * 60.0 / float(bpm)


def from_manifest(info: dict, *, title: str | None = None) -> tuple[Project, dict]:
    """Build the project a bundle describes. Returns `(project, report)`.

    `info` is `load_manifest()`'s dict. `report` carries what the compiler had to
    decide and every bundle warning it saw, so a caller never has to guess why an
    item is missing.
    """
    bundle = Path(info["bundle_dir"])
    bpm = float((info.get("grid") or {}).get("bpm_project") or info["bpm"])
    meter = (int(info["meter"][0]), int(info["meter"][1]))
    duration_s = float(info["duration_s"])
    origin_s = float(info["audio_start_s"])
    audio_offset_s = float(info["audio_source_offset_s"])
    title = title or info.get("title") or bundle.name

    warnings: list[str] = list(info.get("warnings") or [])
    report: dict = {
        "bundle_dir": str(bundle),
        "bpm": bpm,
        "meter": meter,
        "origin_s": origin_s,
        "audio_offset_s": audio_offset_s,
        "warnings": warnings,
    }

    # --- track names, in the fill's own order -------------------------------
    track_names: list[str] = list(BASE_AI_TRACKS)
    if not info.get("vocal_midi_path") and "MIDI_VOCAL" in track_names:
        track_names.remove("MIDI_VOCAL")
    if not info.get("ins_midi_path") and "MIDI_INS" in track_names:
        track_names.remove("MIDI_INS")
    stems = info.get("stems") or {}
    for stem_key, track_name in STEM_TRACK_MAP:
        if stem_key in stems and track_name not in track_names:
            track_names.append(track_name)
    extra_midi = info.get("extra_midi") or {}
    for midi_key, track_name in MIDI_TRACK_MAP:
        if midi_key in extra_midi and track_name not in track_names:
            track_names.append(track_name)
    parts = info.get("midi_parts") or {}
    for part, track_name in DRUM_PART_TRACK_MAP:
        if part in parts and track_name not in track_names:
            track_names.append(track_name)

    # --- the warp plan every audio item shares ------------------------------
    raw_markers, warp_info = _load_warp(bundle)
    warp_markers: list[StretchMarker] = []
    for marker in raw_markers:
        try:
            warp_markers.append(
                StretchMarker(
                    project_pos_s=float(marker["project_s"]),
                    source_pos_s=float(marker["source_s"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            warnings.append("warp.json carries a marker without project_s/source_s; it was dropped")
    warp_markers.sort(key=lambda marker: marker.project_pos_s)
    report["warp"] = {**warp_info, "used": len(warp_markers)}
    audio_length_s = _audio_length(duration_s, audio_offset_s, warp_markers)
    report["audio_length_s"] = audio_length_s

    # --- items, in INSERTION order (that order IS the IID numbering) --------
    items_by_track: dict[str, list[Item]] = {name: [] for name in track_names}
    insertion: list[tuple[str, Item]] = []

    def add(track_name: str, item: Item) -> None:
        insertion.append((track_name, item))
        items_by_track[track_name].append(item)

    audio_sources: list[tuple[str, str]] = [("AI_MASTER", info["audio_path"])]
    for stem_key, track_name in STEM_TRACK_MAP:
        if stem_key in stems:
            audio_sources.append((track_name, stems[stem_key]))
    for track_name, path in audio_sources:
        if track_name not in items_by_track:
            warnings.append("the bundle names a stem for %s but the track was not created" % track_name)
            continue
        add(
            track_name,
            Item(
                position_s=origin_s,
                length_s=audio_length_s,
                source=AudioSource(file_path=str(path)),
                source_offset_s=audio_offset_s,
                name=Path(path).name,
                stretch_markers=tuple(warp_markers),
            ),
        )
    report["audio_items"] = len(audio_sources)

    midi_sources: list[tuple[str, str, str | None]] = []
    if info.get("vocal_midi_path"):
        midi_sources.append(("MIDI_VOCAL", info["vocal_midi_path"], "vocal"))
    if info.get("ins_midi_path"):
        midi_sources.append(("MIDI_INS", info["ins_midi_path"], None))
    for midi_key, track_name in MIDI_TRACK_MAP:
        if midi_key in extra_midi:
            midi_sources.append((track_name, extra_midi[midi_key], midi_key))
    for part, track_name in DRUM_PART_TRACK_MAP:
        if part in parts:
            midi_sources.append((track_name, parts[part], "drums_%s" % part))

    segments_by_artifact = info.get("midi_segments") or {}
    report["midi_items"] = 0
    for track_name, path, artifact in midi_sources:
        if track_name not in items_by_track:
            warnings.append("the bundle names %s but the track was not created" % track_name)
            continue
        midi = read_midi_file(path)
        file_name = Path(path).name
        name = take_name(midi, file_name)
        rows = segments_by_artifact.get(artifact) if artifact else None
        if rows:
            for row in rows:
                start_s = float(row["start_s"])
                end_s = float(row["end_s"])
                add(
                    track_name,
                    Item(
                        position_s=origin_s + start_s,
                        length_s=end_s - start_s,
                        source=MidiSource(
                            ppq=midi.ppq,
                            events=_events_for_window(midi, bpm, start_s, end_s),
                            end_tick=midi.end_tick,
                        ),
                        source_offset_s=start_s,
                        name=name,
                    ),
                )
                report["midi_items"] += 1
            continue
        add(
            track_name,
            Item(
                position_s=origin_s,
                length_s=_midi_whole_length(midi, bpm),
                source=MidiSource(
                    ppq=midi.ppq,
                    events=_events_for_window(midi, bpm, None, None),
                    end_tick=midi.end_tick,
                ),
                source_offset_s=0.0,
                name=name,
            ),
        )
        report["midi_items"] += 1

    # IID is the project-wide insertion number; the model carries it explicitly
    # so the writer does not have to guess (measured: 1-6 audio, then MIDI).
    for number, (_track_name, item) in enumerate(insertion, start=1):
        item.iid = number

    tracks = tuple(Track(name=name, items=tuple(items_by_track[name])) for name in track_names)

    regions = tuple(
        Region(name=str(region["name"]), start_s=float(region["start_s"]), end_s=float(region["end_s"]))
        for region in (info.get("regions") or [])
    )
    project = Project(
        title=title,
        tempo_bpm=bpm,
        meter=meter,
        sample_rate=int(info.get("sample_rate") or 44100),
        tracks=tracks,
        markers=(Marker(name=title, pos_s=0.0),),
        regions=regions,
    )
    report["tracks"] = len(tracks)
    report["items"] = len(insertion)
    report["regions"] = len(regions)
    return project, report
