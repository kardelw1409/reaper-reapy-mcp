"""audiocpp.manifest v1: load, validate, resolve - pure Python, no reapy.

The canonical contract is docs/audiocpp_manifest.md (synchronized copy from
mcp-audiocpp). Everything here is offline-testable: the reapy mutations live in
apply.py. The exported bundle carries relative filenames only; this module
resolves them against the bundle directory and refuses anything malformed
(fail-closed on schema/version, forward-compatible on unknown fields).
"""

from __future__ import annotations

import json
import os
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

#: Every name the double-apply guard matches (base + all optional tracks).
AI_TRACKS: tuple[str, ...] = BASE_AI_TRACKS + ("AI_DRUMS", "AI_BASS", "AI_OTHER", "MIDI_DRUMS", "MIDI_BASS")

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
    vocal_path = _require_file(bundle, midi.get("vocal"), what="vocal MIDI")
    ins_path = _require_file(bundle, midi.get("ins"), what="instrumental MIDI")
    extra_midi: dict[str, str] = {}
    for key in MIDI_TRACK_MAP:
        if midi.get(key):
            extra_midi[key] = _require_file(bundle, midi[key], what=f"{key} MIDI")

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
    }

    regions = _normalize_regions(doc)

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
        "stems": stems,
        "grid": grid,
        "midi_sources": doc.get("midi_sources") or {},
        "regions": regions,
        "warnings": [str(w) for w in (doc.get("warnings") or [])],
    }


def clamp_regions(
    regions: list[dict],
    duration_s: float,
    *,
    allow_overhang_s: float = 0.0,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Clamp region ends to the audio end; drop regions that start past it.

    Returns (kept, clamps, skipped, overhangs). `duration_s` is the end of the
    audio in PROJECT time (audio.start_s + duration). A region whose end lands
    INSIDE `allow_overhang_s` of that end keeps its end (a bar-aligned edge may
    legitimately poke a little past the audio) and is listed in `overhangs`;
    anything further is clamped and recorded in `clamps`. A region starting at
    or past the end cannot sound and is skipped.
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
        if region["start_s"] >= end - 1e-9:
            # Cannot sound: skipped, and NOT reported as a clamp (a region that
            # does not survive must not show up in two lists).
            skipped.append({**region, "applied_end_s": round(max(end, 0.0), 3)})
            continue
        if end < region["end_s"] - 1e-9:
            clamps.append(
                {"name": region["name"], "end_s": region["end_s"], "applied_end_s": round(end, 3)}
            )
        kept.append({**region, "end_s": round(end, 3)})
    return kept, clamps, skipped, overhangs
