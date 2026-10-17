"""audiocpp.manifest v1: load, validate, resolve - pure Python, no reapy.

The canonical contract is docs/audiocpp_manifest.md (synchronized copy from
mcp-audiocpp). Everything here is offline-testable: the reapy mutations live in
apply.py. The exported bundle carries relative filenames only; this module
resolves them against the bundle directory and refuses anything malformed
(fail-closed on schema/version, forward-compatible on unknown fields).
"""

from __future__ import annotations

import json
import math
import os
import statistics
from pathlib import Path

SCHEMA = "audiocpp.manifest"
VERSION = 1

MANIFEST_FILENAME = "manifest.json"
APPLY_REPORT_FILENAME = "apply_report.json"

#: The five tracks a v1.0 bundle always creates (AI_VOCAL/AI_INSTR stay empty
#: when the bundle carries no separated vocal/instrumental stem).
BASE_AI_TRACKS: tuple[str, ...] = ("AI_MASTER", "AI_VOCAL", "AI_INSTR", "MIDI_VOCAL", "MIDI_INS")

#: Extra tracks, created only when the bundle carries the matching artifact
#: (contract extension: stems and transcribed MIDI are optional keys).
STEM_TRACK_MAP: dict[str, str] = {
    "vocals": "AI_VOCAL",       # fills the base track that v1.0 left empty
    "instrumental": "AI_INSTR",  # ditto
    "drums": "AI_DRUMS",
    "bass": "AI_BASS",
    "other": "AI_OTHER",
}
MIDI_TRACK_MAP: dict[str, str] = {
    "drums": "MIDI_DRUMS",
    "bass": "MIDI_BASS",
}

#: One track PER drum class, so the arrangement gets kick/snare/hihat/tom/cymbal as
#: separate items instead of one merged drum part (the whole point of the per-class
#: files mcp-stem-splitter :: drums_midi writes). Populated only when the bundle
#: carries `midi_parts`.
DRUM_PART_TRACK_MAP: dict[str, str] = {
    "kick": "MIDI_DRUMS_KICK",
    "snare": "MIDI_DRUMS_SNARE",
    "hihat": "MIDI_DRUMS_HIHAT",
    "tom": "MIDI_DRUMS_TOM",
    "cymbal": "MIDI_DRUMS_CYMBAL",
}

#: Every name the double-apply guard matches (base + all optional tracks).
AI_TRACKS: tuple[str, ...] = (
    BASE_AI_TRACKS
    + ("AI_DRUMS", "AI_BASS", "AI_OTHER", "MIDI_DRUMS", "MIDI_BASS")
    + tuple(DRUM_PART_TRACK_MAP.values())
)

VALID_SOURCES = ("plan", "audio", "mixed")

#: Default location of the export-side pointer (overridable via env).
DEFAULT_POINTER = Path(r"C:\repos\mcp-audiocpp\work\_latest_manifest.txt")


class ManifestError(ValueError):
    """The bundle cannot be used; the message is user-facing."""

    def __init__(self, message: str, *, detail: str | None = None, hints: list[str] | None = None):
        super().__init__(message)
        self.detail = detail
        self.hints = hints or []


def pointer_path() -> Path:
    """Location of the 'latest bundle' pointer (env AUDIOCPP_MANIFEST_POINTER wins)."""
    override = os.environ.get("AUDIOCPP_MANIFEST_POINTER")
    return Path(override) if override else DEFAULT_POINTER


def resolve_bundle_dir(bundle_dir: str | None) -> Path:
    """'latest'/None -> the pointer target; otherwise the directory as given."""
    if bundle_dir in (None, "", "latest"):
        pointer = pointer_path()
        try:
            text = pointer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ManifestError(
                "no 'latest' bundle pointer found",
                detail=f"{pointer}: {exc}",
                hints=[
                    "export a bundle first (mcp-audiocpp :: export_audiocpp_manifest)",
                    "or pass bundle_dir explicitly",
                    "the pointer path is overridable via AUDIOCPP_MANIFEST_POINTER",
                ],
            ) from exc
        if not text:
            raise ManifestError("the latest-bundle pointer is empty", detail=str(pointer))
        return Path(text)
    return Path(bundle_dir)


def _require_number(doc: dict, key: str, *, positive: bool = True) -> float:
    value = doc.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ManifestError(f"manifest {key!r} must be a number")
    if positive and value <= 0:
        raise ManifestError(f"manifest {key!r} must be positive", detail=f"got {value!r}")
    return float(value)


def _optional_file(bundle: Path, name: object, *, what: str) -> str | None:
    """A file the manifest may legitimately NOT name - unlike the mandatory ones.

    `midi.vocal` and `midi.ins` come from the SCORE, and a score can be unreadable while the rest of
    the take is perfectly fine: measured on a progressive-house render, YuE2 wrote a truncated chord
    token as the whole Vocal line of one group of forty-six, so no score-derived MIDI could be written
    while the audio, the regions and the transcribed drum parts were all intact. Absence therefore
    means "no item for this artifact" - which is NOT the same as naming a file that is not there: that
    stays fatal (`_require_file`), because it means the bundle is incomplete.
    """
    if name in (None, ""):
        return None
    return _require_file(bundle, name, what=what)


def _require_file(bundle: Path, name: object, *, what: str) -> str:
    if not isinstance(name, str) or not name:
        raise ManifestError(f"manifest is missing the {what} filename")
    path = (bundle / name).resolve()
    if not path.exists():
        raise ManifestError(
            f"the {what} file referenced by the manifest does not exist",
            detail=str(path),
            hints=["the bundle is incomplete - re-export it"],
        )
    return str(path)


def _normalize_regions(doc: dict) -> list[dict]:
    regions = doc.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ManifestError("manifest regions must be a non-empty list")
    out: list[dict] = []
    for i, region in enumerate(regions):
        if not isinstance(region, dict):
            raise ManifestError(f"region {i} is not an object")
        name = region.get("name")
        start = region.get("start_s")
        end = region.get("end_s")
        source = region.get("source")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"region {i} has no name")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ManifestError(f"region {i} ({name!r}) start_s/end_s must be numbers")
        if not start < end:
            raise ManifestError(
                f"region {i} ({name!r}) end_s must be greater than start_s",
                detail=f"start_s={start}, end_s={end}",
            )
        if source not in VALID_SOURCES:
            raise ManifestError(
                f"region {i} ({name!r}) has an unknown source {source!r}",
                detail=f"valid sources: {', '.join(VALID_SOURCES)}",
            )
        out.append({"name": name, "start_s": float(start), "end_s": float(end), "source": source})
    return out


def load_manifest(bundle_dir: str | Path) -> dict:
    """Read + validate manifest.json; returns resolved absolute artifact paths."""
    bundle = Path(bundle_dir).resolve()
    manifest_path = bundle / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ManifestError(f"manifest.json not found in {bundle}")
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest.json is not readable JSON", detail=str(exc)) from exc
    if not isinstance(doc, dict):
        raise ManifestError("manifest.json must be a JSON object")
    if doc.get("schema") != SCHEMA:
        raise ManifestError(
            f"unknown manifest schema {doc.get('schema')!r}",
            detail=f"expected {SCHEMA!r}",
        )
    if doc.get("version") != VERSION:
        raise ManifestError(
            f"unsupported manifest version {doc.get('version')!r}",
            detail=f"this build applies version {VERSION}",
            hints=["re-export the bundle with a matching mcp-audiocpp"],
        )

    bpm = _require_number(doc, "bpm")
    duration_s = _require_number(doc, "duration_s")

    meter = doc.get("meter")
    if (
        not isinstance(meter, list)
        or len(meter) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in meter)
    ):
        raise ManifestError("manifest meter must be [numerator, denominator] (positive ints)")

    audio = doc.get("audio")
    if not isinstance(audio, dict):
        raise ManifestError("manifest is missing the audio object")
    audio_path = _require_file(bundle, audio.get("file"), what="audio")
    audio_start_s = audio.get("start_s", 0.0)
    if not isinstance(audio_start_s, (int, float)) or isinstance(audio_start_s, bool):
        raise ManifestError("manifest audio.start_s must be a number")
    source_offset_s = audio.get("source_offset_s", 0.0)
    if not isinstance(source_offset_s, (int, float)) or isinstance(source_offset_s, bool):
        raise ManifestError("manifest audio.source_offset_s must be a number")
    if source_offset_s < 0:
        raise ManifestError(
            "manifest audio.source_offset_s must not be negative", detail=f"got {source_offset_s}"
        )

    midi = doc.get("midi")
    if not isinstance(midi, dict):
        raise ManifestError("manifest is missing the midi object")
    vocal_path = _optional_file(bundle, midi.get("vocal"), what="vocal MIDI")
    ins_path = _optional_file(bundle, midi.get("ins"), what="instrumental MIDI")
    extra_midi: dict[str, str] = {}
    for key in MIDI_TRACK_MAP:
        if midi.get(key):
            extra_midi[key] = _require_file(bundle, midi[key], what=f"{key} MIDI")

    midi_time: dict[str, dict] = {}
    for key, value in (doc.get("midi_time") or {}).items():
        if isinstance(value, dict) and "notes" in value:
            midi_time[str(key)] = value

    midi_parts: dict[str, str] = {}
    parts_raw = doc.get("midi_parts") or {}
    if not isinstance(parts_raw, dict):
        raise ManifestError("manifest midi_parts must be an object")
    for group, parts in parts_raw.items():
        if not isinstance(parts, dict):
            raise ManifestError(f"manifest midi_parts[{group!r}] must be an object of part -> file")
        for part, name in parts.items():
            if part not in DRUM_PART_TRACK_MAP:
                raise ManifestError(
                    f"unknown drum part {part!r} in the bundle",
                    detail=f"valid parts: {', '.join(DRUM_PART_TRACK_MAP)}",
                )
            midi_parts[part] = _require_file(bundle, name, what=f"{part} MIDI")

    stems_raw = doc.get("stems") or {}
    if not isinstance(stems_raw, dict):
        raise ManifestError("manifest stems must be an object")
    stems: dict[str, str] = {}
    for key, name in stems_raw.items():
        if key not in STEM_TRACK_MAP:
            raise ManifestError(
                f"unknown stem {key!r} in the bundle",
                detail=f"valid stems: {', '.join(STEM_TRACK_MAP)}",
            )
        stems[key] = _require_file(bundle, name, what=f"{key} stem")

    grid_raw = doc.get("grid") or {}
    if not isinstance(grid_raw, dict):
        raise ManifestError("manifest grid must be an object")
    bar_s = grid_raw.get("bar_s")
    if bar_s is not None and (not isinstance(bar_s, (int, float)) or bar_s <= 0):
        raise ManifestError("manifest grid.bar_s must be a positive number")
    grid = {
        "quantize": str(grid_raw.get("quantize") or "none"),
        "bar_s": float(bar_s) if bar_s else None,
        "aligned": bool(grid_raw.get("aligned")),
        "first_downbeat_s": float(grid_raw.get("first_downbeat_s") or 0.0),
        "max_shift_s": grid_raw.get("max_shift_s"),
        "source": grid_raw.get("source"),
        "bpm_measured": grid_raw.get("bpm_measured"),
        # The tempo the PROJECT must run at. The score's bpm and the render's measured
        # tempo differ by ~78 ppm on the DnB take, which drags the music 21 ms off the
        # ruler over 270 s if the score's value is used for the ruler as well.
        "bpm_project": (float(grid_raw["bpm_project"]) if grid_raw.get("bpm_project") else None),
        "pre_roll_s": (float(grid_raw["pre_roll_s"]) if grid_raw.get("pre_roll_s") else None),
    }

    regions = _normalize_regions(doc)

    # WHERE EACH PART ACTUALLY PLAYS (audiocpp `parts.normalize_parts` -> `segments`, recorded by
    # the exporter as `midi_segments`): one row per region that holds notes, in PROJECT seconds
    # already (`total_shift_s` was applied at export). The fill inserts ONE ITEM PER ROW instead of
    # one item per file, which is the difference between a part that plays in the chorus and a
    # block of MIDI lying across the whole arrangement. A bundle without the block keeps the old
    # behaviour, and the report says which happened.
    midi_segments: dict[str, list[dict]] = {}
    segments_raw = doc.get("midi_segments") or {}
    if not isinstance(segments_raw, dict):
        raise ManifestError("manifest midi_segments must be an object")
    for artifact, block in segments_raw.items():
        rows = (block or {}).get("segments") if isinstance(block, dict) else block
        if not isinstance(rows, list):
            continue
        clean: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                start = float(row["start_s"])
                end = float(row["end_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            clean.append(
                {
                    "start_s": start,
                    "end_s": end,
                    "notes": int(row.get("notes") or 0),
                    "name": str(row.get("name") or ""),
                }
            )
        if clean:
            midi_segments[str(artifact)] = clean

    return {
        "bundle_dir": str(bundle),
        "manifest_path": str(manifest_path),
        "doc": doc,
        "title": str(doc.get("title") or "untitled"),
        "bpm": bpm,
        "meter": [int(meter[0]), int(meter[1])],
        "duration_s": duration_s,
        "audio_path": audio_path,
        "audio_start_s": float(audio_start_s),
        "audio_source_offset_s": float(source_offset_s),
        "vocal_midi_path": vocal_path,
        "ins_midi_path": ins_path,
        "extra_midi": extra_midi,
        "midi_parts": midi_parts,
        "stems": stems,
        "grid": grid,
        "midi_sources": doc.get("midi_sources") or {},
        "midi_time": midi_time,
        "midi_segments": midi_segments,
        "regions": regions,
        "warnings": [str(w) for w in (doc.get("warnings") or [])],
    }


#: Which track each MIDI artifact goes to (the base two live outside this map).
MIDI_ARTIFACT_TRACK: dict[str, str] = {
    "vocal": "MIDI_VOCAL",
    "ins": "MIDI_INS",
    **MIDI_TRACK_MAP,
    **{f"drums_{part}": track for part, track in DRUM_PART_TRACK_MAP.items()},
}

#: A note in REAPER is placed on a PPQ grid, so an exact match is not available.
#: 20 ms covers the tick rounding of a 220-ppq file at 172 bpm (~1.6 ms per tick)
#: plus the report's own 4-decimal rounding, and still catches a tempo error - the
#: defect this check exists for put the last drum note 80.9 s early.
NOTE_TIME_TOLERANCE_S = 0.02


def expected_note_spans(info: dict) -> dict[str, dict]:
    """Per MIDI artifact: the note count and first/last note in PROJECT seconds.

    The exporting side writes this into `manifest.midi_time`, and it is the only
    place that knows both the tempo the file was WRITTEN at and the project's. A
    bundle exported before that block existed has no expectations, so the caller
    must be told that nothing was checked instead of being handed a pass.
    """
    out: dict[str, dict] = {}
    for key, value in (info.get("midi_time") or {}).items():
        notes = value.get("notes")
        if not isinstance(notes, int):
            continue
        out[key] = {
            "notes": notes,
            "first_s": value.get("first_note_project_s"),
            "last_s": value.get("last_note_project_s"),
            "file_bpm": value.get("file_bpm"),
            "source": value.get("bar_offset_source"),
        }
    return out


def compare_note_spans(
    expected: dict[str, dict],
    actual: dict[str, dict],
    *,
    tolerance_s: float = NOTE_TIME_TOLERANCE_S,
) -> tuple[list[str], dict]:
    """Compare what the project HOLDS against what the manifest says it should.

    This is the check that would have caught a 120 bpm artifact in a 172 bpm
    project at apply time: the note count matches either way (1176 notes are 1176
    notes), but the last note lands at 186.7 s instead of 267.7 s. Counting notes
    alone, which is what the first version did, cannot see that.

    Returns (warnings, report). A missing measurement is reported as "not
    checked", never as a pass.
    """
    warnings: list[str] = []
    report: dict[str, dict] = {}
    for key in sorted(expected):
        want = expected[key]
        got = actual.get(key)
        if got is None:
            report[key] = {"checked": False, "reason": "no measurement from the project"}
            warnings.append(f"MIDI '{key}': could not read the notes back - not checked")
            continue
        entry: dict = {"checked": True, "expected_notes": want["notes"], "notes": got.get("notes")}
        problems: list[str] = []
        if got.get("notes") != want["notes"]:
            problems.append(f"notes {got.get('notes')} != {want['notes']}")
        for label, field in (("first", "first_s"), ("last", "last_s")):
            want_s, got_s = want.get(field), got.get(field)
            entry[f"{label}_s"] = got_s
            entry[f"expected_{label}_s"] = want_s
            if want_s is None or got_s is None:
                continue
            delta = float(got_s) - float(want_s)
            entry[f"{label}_delta_s"] = round(delta, 4)
            if abs(delta) > tolerance_s:
                problems.append(f"{label} note {got_s:.3f} s vs {want_s:.3f} s (delta {delta:+.3f} s)")
        if problems:
            entry["ok"] = False
            entry["problems"] = problems
            warnings.append(f"MIDI '{key}' does not match the manifest: " + "; ".join(problems))
        else:
            entry["ok"] = True
        report[key] = entry
    return warnings, report


GRID_PHASE_WINDOW_S = 0.025
GRID_PHASE_MIN_SHARE = 0.8


def summarize_note_phase(deviations: list[float], step_s: float) -> dict:
    """How far a set of note deviations sits from the grid - and whether it can be said at all.

    `phase_ms` is only a GRID OFFSET when the notes actually share a phase. Measured on the
    real bundle's bass: its deviations are spread over the whole 1/16 step (a flat 10 ms
    histogram), so the median is arbitrary - the SAME file shifted by 8.47 ms moved that
    median by 4.4 ms (-3.58 -> -7.94), while every note had moved by exactly the shift.
    Reporting that number as "the part sits 7.9 ms off the grid" would be inventing a fact
    out of a median. Hence `phase_is_an_offset`, decided by the share of notes within the
    window, and a `note` that says what the number is when it is not an offset.
    """
    n = len(deviations)
    ordered = sorted(deviations)
    phase = statistics.median(ordered)
    share = sum(1 for d in ordered if abs(d - phase) <= GRID_PHASE_WINDOW_S) / n
    p25 = ordered[int(0.25 * (n - 1))]
    p75 = ordered[int(0.75 * (n - 1))]
    on_a_grid = share >= GRID_PHASE_MIN_SHARE
    entry: dict = {
        "notes": n,
        "phase_ms": round(phase * 1000.0, 2),
        "share_on_grid": round(share, 3),
        "spread_ms": round((p75 - p25) * 1000.0, 2),
        "step_s": round(step_s, 6),
        "phase_is_an_offset": on_a_grid,
    }
    if not on_a_grid:
        entry["note"] = (
            "these notes are spread across the whole step, so phase_ms is the median of a FLAT "
            "distribution and is NOT how far the part sits off the grid (measured: the same "
            "file moved by a rigid 8.5 ms changed this median by 4.4 ms)"
        )
    return entry


def note_report_ok(note_report: dict) -> bool:
    """Whether every MEASURED artifact matched, ignoring the bookkeeping entries.

    Keys starting with "_" are notes *about* the report (`_checked`, `_superseded`),
    not measurements: `_superseded` has no "ok" field at all, and `all()` reads an
    absent value as False. That is how a fully successful apply reported
    `ok: false` while every one of its eight artifacts said `ok: true`.

    An entry that says `checked: False` still counts as a failure - a verification
    that did not happen is not a pass, and it must not be possible to make this
    return True by having nothing to compare.

    Whether there was anything to check at all is a DIFFERENT question, answered by
    `verification.notes_checked`: "every measurement matched" is not "a measurement
    was made". Entries that are not measurement-shaped (a bare reason string) cannot
    be judged, so they are skipped rather than crashing the caller.
    """
    return all(
        entry.get("ok")
        for key, entry in note_report.items()
        if not key.startswith("_") and isinstance(entry, dict)
    )


def clamp_regions(
    regions: list[dict],
    duration_s: float,
    *,
    allow_overhang_s: float = 0.0,
    snap_s: float = 0.0,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Clamp region ends to the audio end; drop regions that start past it.

    Returns (kept, clamps, skipped, overhangs). `duration_s` is the end of the
    audio in PROJECT time (audio.start_s + duration). A region whose end lands
    INSIDE `allow_overhang_s` of that end keeps its end (a bar-aligned edge may
    legitimately poke a little past the audio) and is listed in `overhangs`;
    anything further is clamped and recorded in `clamps`. A region starting at
    or past the end cannot sound and is skipped.

    `snap_s` is the grid the clamp itself must land on (a bar). Clamping to the
    audio's exact end is what put a region edge 572 ms off the ruler on the real
    DnB take: the audio does not stop on a bar line, so a cut there is off the
    grid by construction. With `snap_s` the clamped end is the first grid line at
    or after the audio's end - still covering the audio, and still on the ruler.
    """
    kept: list[dict] = []
    clamps: list[dict] = []
    skipped: list[dict] = []
    overhangs: list[dict] = []
    for region in regions:
        end = region["end_s"]
        if end > duration_s + 1e-9:
            if end - duration_s <= allow_overhang_s + 1e-9:
                overhangs.append(
                    {
                        "name": region["name"],
                        "end_s": end,
                        "audio_end_s": round(duration_s, 3),
                        "overhang_s": round(end - duration_s, 3),
                    }
                )
            else:
                end = duration_s
                if snap_s and snap_s > 0:
                    end = math.ceil(end / snap_s - 1e-9) * snap_s
        if region["start_s"] >= end - 1e-9:
            # Cannot sound: skipped, and NOT reported as a clamp (a region that
            # does not survive must not show up in two lists).
            skipped.append({**region, "applied_end_s": round(max(end, 0.0), 3)})
            continue
        if end < region["end_s"] - 1e-9:
            clamps.append(
                {"name": region["name"], "end_s": region["end_s"], "applied_end_s": round(end, 6)}
            )
        # MICROSECONDS, not milliseconds: a region edge is a BAR LINE, and rounding it to
        # 0.001 s moves it off the ruler by up to 0.5 ms - which is what the live project
        # showed (0.47 ms worst deviation) before this was found.
        kept.append({**region, "end_s": round(end, 6)})
    return kept, clamps, skipped, overhangs
