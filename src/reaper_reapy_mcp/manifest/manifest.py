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

#: The tracks the fill creates; also the names the double-apply guard matches.
AI_TRACKS: tuple[str, ...] = ("AI_MASTER", "AI_VOCAL", "AI_INSTR", "MIDI_VOCAL", "MIDI_INS")

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

    midi = doc.get("midi")
    if not isinstance(midi, dict):
        raise ManifestError("manifest is missing the midi object")
    vocal_path = _require_file(bundle, midi.get("vocal"), what="vocal MIDI")
    ins_path = _require_file(bundle, midi.get("ins"), what="instrumental MIDI")

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
        "vocal_midi_path": vocal_path,
        "ins_midi_path": ins_path,
        "regions": regions,
        "warnings": [str(w) for w in (doc.get("warnings") or [])],
    }


def clamp_regions(regions: list[dict], duration_s: float) -> tuple[list[dict], list[dict], list[dict]]:
    """Clamp region ends to the audio duration; drop regions that start past it.

    Returns (kept, clamps, skipped). A clamp is recorded when the applied end is
    earlier than the manifest's end; a region whose start_s is already at/past the
    duration cannot sound its range and is skipped with a record.
    """
    kept: list[dict] = []
    clamps: list[dict] = []
    skipped: list[dict] = []
    for region in regions:
        end = min(region["end_s"], duration_s)
        if region["start_s"] >= end - 1e-9:
            skipped.append({**region, "applied_end_s": round(max(end, 0.0), 3)})
            continue
        if end < region["end_s"] - 1e-9:
            clamps.append(
                {"name": region["name"], "end_s": region["end_s"], "applied_end_s": round(end, 3)}
            )
        kept.append({**region, "end_s": round(end, 3)})
    return kept, clamps, skipped
