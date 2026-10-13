"""Vital's wavetable channel: **which table** an oscillator reads (P30).

THE MEASURED MODEL
------------------
Vital's UI has a wavetable selector and **there is no wavetable parameter**: the
775-control inventory holds exactly three controls whose names mention `wave`,
and all three are ``Oscillator N Wave Frame`` - the **position inside** the
current table. The selection lives in the plugin's serialized document::

    settings.wavetables = [ <table for Osc 1>, <table for Osc 2>, <table for Osc 3> ]

    <table> = {"author": str, "name": str, "version": str,
               "full_normalize": bool, "remove_all_dc": bool,
               "groups": [{"components": [<component>, ...]}]}

    <component> = {"type": "Wave Source" | "Wave Folder" | ...,
                   "interpolation": int, "interpolation_style": int,
                   "keyframes": [{"position": int, "wave_data": <base64 float32>}, ...]}

That model is a **verbatim port** of the POC's `adapter/wavetables.py` (the same
project's 2026-09-29 measurement; `17 AV-13` verified that module host-free), and it
is corroborated independently by two checks this round: the POC's extracted INIT
resource decodes to a perfect sawtooth (h2 = 0.500, h3 = 0.333 - measured offline),
and the corpus of 2,226 preset files carries tables in exactly this shape.

THE TWO RULES THIS MODULE EXISTS FOR (both measured, 2026-09-29)
----------------------------------------------------------------
1. **A table is identified by its CONTENT, never by its ``name``.** The live
   instance holds three tables all labelled ``"Init"`` while 112 distinct
   single-frame tables in the preset corpus carry that same name - a label is not
   an identity, a digest is.
2. 🔴 **A GROWING table write must be refused.** A single-frame replacement
   (11,159 -> 11,149 bytes) was **committed and verified** live, both directions.
   An 8-frame / 87,855-byte payload did **not land at all** and, with a `version`
   that disagreed with the plugin's, **aborted `reaper.exe`** (faulting module
   `ucrtbase.dll`, exception `0xc0000409` = `STATUS_STACK_BUFFER_OVERRUN`). The
   rule is therefore RELATIVE, and it refuses growth in **frames**, **total
   samples** (the buffer the hypothesis names) and **wave-carrying content
   bytes**, while the cosmetic fields (`name`/`author`/`version`) stay free; the
   raw payload is capped at the one measured-safe size. `version` is stamped from
   the live document.

The plugin **restamps ``version`` on re-serialisation** (measured: the only
differing field on round-trip). A verification that compares the written value
with the read-back therefore reports a landed write as a failure unless the value
is stamped with what the plugin will write - which is what :func:`stamp_version`
does, and why a missing live version is a **refusal** rather than a guess.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import struct
from typing import Any

from ...errors import InvalidValue, Unreadable

__all__ = [
    "SLOTS",
    "SETTINGS_KEY",
    "SOURCE_COMPONENT",
    "FRAME_AXIS_MAX",
    "HOST_ABORT_EVIDENCE",
    "VERIFIED_PAYLOAD_BYTES",
    "path_for",
    "check_slot",
    "parse_wavetable_path",
    "components",
    "source_component",
    "frame_positions",
    "sample_count",
    "total_samples",
    "content_bytes",
    "table_id",
    "payload_bytes",
    "describe",
    "describe_slots",
    "check_writable",
    "stamp_version",
    "live_version",
    "read_preset_tables",
    "table_from_preset",
    "read_slot_from_preset",
]

#: The oscillator slots a wavetable belongs to. Vital has three; the sample layer is
#: not here on purpose - it has its own engine and no wavetable.
SLOTS = (1, 2, 3)

#: The document key. The Adapter may know it because it describes *Vital*: the
#: parameter array cannot express "which table", and this is where the table lives.
SETTINGS_KEY = "wavetables"

#: The component type that carries an audible table. The other measured types are
#: processing stages in the table's own graph (`Wave Folder`, `Frequency Filter`,
#: `Audio File Source`, `Phase Shift`, `Wave Window`, `Line Source`, `Wave Warp`).
SOURCE_COMPONENT = "Wave Source"

#: The frame axis is 0..256 in every table measured (`kNumOscillatorWaveFrames`).
FRAME_AXIS_MAX = 256

#: The payload measured safe on the live build: the INIT table a fresh instance
#: holds (1 frame, 2048 samples, 11,159 serialized bytes). Kept as a number WITH its
#: provenance so a later phase can widen it by measurement rather than by editing a
#: threshold.
VERIFIED_PAYLOAD_BYTES = 11_159

#: What the two measured attempts establish, and what they do not separate. Named so
#: a refusal can carry its own evidence and a later phase knows the exact question.
HOST_ABORT_EVIDENCE: dict[str, Any] = {
    "established": [
        "a non-growing single-frame replacement LANDS: 11,159 -> 11,149 -> 11,159 bytes, "
        "committed and read-back verified through the state chunk",
        "a growing payload does NOT land: stage verify, the plugin's own read-back is "
        "the unchanged table, 0 controls moved",
        "a growing payload whose `version` disagreed with the plugin's ABORTED reaper.exe "
        "once (ucrtbase.dll, 0xc0000409 STATUS_STACK_BUFFER_OVERRUN)",
        "the plugin restamps `version` on re-serialisation, so a value not stamped with "
        "the live version cannot be verified",
    ],
    "hypothesis": (
        "the plugin's state deserialiser sizes its wavetable buffer from what the engine "
        "ALREADY holds, so a growing payload cannot be stored - and, with a mismatched "
        "`version`, could not be parsed safely either"
    ),
    "not_separated": ["total size", "frame count", "the interpolation value"],
    "why_not_separated": (
        "both growing attempts carried an 8-frame table, so size and frame count moved "
        "together; separating them costs another write to a host that has already "
        "aborted once on an adjacent payload"
    ),
    "the_settled_experiment": (
        "load a multi-frame table through the plugin's own browser (so the engine is "
        "sized for it); `Oscillator N Wave Frame` is then scriptable and needs no "
        "document write at all"
    ),
    "provenance": "POC measurement 2026-09-29, mirrors of the evidence in "
                  "C:\\REAPER\\prompts\\vital_sound_design\\logs\\wavetable_proof__*.json",
}


# ---------------------------------------------------------------------------
# Paths and slots
# ---------------------------------------------------------------------------


def path_for(oscillator: Any) -> str:
    """``1 -> settings.wavetables.0``. One place knows the index is zero-based."""
    return f"settings.{SETTINGS_KEY}.{check_slot(oscillator) - 1}"


def check_slot(oscillator: Any) -> int:
    try:
        slot = int(oscillator)
    except (TypeError, ValueError) as exc:
        raise InvalidValue(
            f"{oscillator!r} is not an oscillator slot",
            detail={
                "slots": list(SLOTS),
                "note": "a wavetable belongs to an oscillator; the sample layer has no wavetable",
            },
        ) from exc
    if slot not in SLOTS:
        raise InvalidValue(
            f"oscillator {slot} has no wavetable slot", detail={"slots": list(SLOTS)}
        )
    return slot


def parse_wavetable_path(path: Any) -> dict[str, Any] | None:
    """The wavetable slot a dotted path addresses, or ``None``.

    Recognises ``settings.wavetables.<i>`` and any deeper path under it
    (``settings.wavetables.0.groups.0``). It is deliberately the codec's own
    representation: a caller can reach the same slot by writing the raw state path,
    so a guard that only looked at a semantic operation would be bypassable - exactly
    the reasoning that put the route-pair guard in the codec.
    """
    parts = [segment for segment in str(path).split(".") if segment != ""]
    if len(parts) < 3 or parts[0] != "settings" or parts[1] != SETTINGS_KEY:
        return None
    if not parts[2].isdigit():
        return None
    index = int(parts[2])
    return {
        "path": str(path),
        "index": index,
        "oscillator": index + 1 if (index + 1) in SLOTS else None,
        "suffix": parts[3:],
        "is_slot": len(parts) == 3,
    }


# ---------------------------------------------------------------------------
# Table access
# ---------------------------------------------------------------------------


def _first_group_components(table: Any) -> list[dict[str, Any]]:
    if not isinstance(table, dict):
        return []
    groups = table.get("groups") or []
    if not isinstance(groups, list) or not groups:
        return []
    first = groups[0]
    if not isinstance(first, dict):
        return []
    comps = first.get("components") or []
    return [c for c in comps if isinstance(c, dict)] if isinstance(comps, list) else []


def components(table: Any) -> list[dict[str, Any]]:
    """Every component of the table's first group (the measured layout)."""
    return _first_group_components(table)


def source_component(table: Any) -> dict[str, Any] | None:
    """The component that carries the audible table, or ``None``.

    A table whose components are all processing stages has no wave data to read;
    that is reported as such rather than treated as a single-frame table.
    """
    for comp in _first_group_components(table):
        if comp.get("type") == SOURCE_COMPONENT:
            return comp
    return None


def keyframes(table: Any) -> list[dict[str, Any]]:
    comp = source_component(table)
    if comp is None:
        return []
    raw = comp.get("keyframes") or []
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def frame_positions(table: Any) -> list[int]:
    """The frame positions on the 0..256 axis, in file order."""
    out: list[int] = []
    for row in keyframes(table):
        position = row.get("position")
        if isinstance(position, (int, float)):
            out.append(int(position))
    return out


def _decode_wave_data(value: Any) -> tuple[int, tuple[float, ...]] | None:
    """The decoded float32 samples of one keyframe's ``wave_data``, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
    except Exception:  # noqa: BLE001 - a malformed payload is an ordinary outcome
        return None
    count = len(raw) // 4
    if count <= 0:
        return None
    return count, struct.unpack("<%df" % count, raw[: count * 4])


def sample_count(table: Any) -> int | None:
    """Samples per frame, when the table carries exactly one frame."""
    rows = keyframes(table)
    if len(rows) != 1:
        return None
    return _frame_sample_count(rows[0]) or None


def _frame_sample_count(row: dict[str, Any]) -> int:
    """Samples in one keyframe's ``wave_data`` (decoded byte length / 4)."""
    value = row.get("wave_data")
    if not isinstance(value, str) or not value:
        return 0
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
    except Exception:  # noqa: BLE001 - a malformed payload is an ordinary outcome
        return 0
    return len(raw) // 4


def total_samples(table: Any) -> int:
    """The table's total sample capacity: frames x samples per frame.

    This is the dimension the host-abort hypothesis names ("the deserialiser sizes its
    wavetable buffer from what the engine ALREADY holds"), so it is measured as its own
    number instead of being folded into the serialized size.
    """
    return sum(_frame_sample_count(row) for row in keyframes(table))


COSMETIC_FIELDS: tuple[str, ...] = ("name", "author", "version")


def content_bytes(table: Any) -> int:
    """Serialized size with the cosmetic fields blanked: the wave-carrying size.

    A table's ``name`` is a label (the live instance holds three tables all called
    "Init"), so a three-character-longer name must not read as "growth". Everything
    else - component graph, keyframe positions, the base64 sample payload - is content
    and is compared at full size.
    """
    if not isinstance(table, dict):
        return payload_bytes(table)
    stripped = copy.deepcopy(table)
    for key in COSMETIC_FIELDS:
        if key in stripped:
            stripped[key] = ""
    return payload_bytes(stripped)


def table_id(table: Any) -> str | None:
    """A CONTENT identity for a table: a digest of its frame data.

    ``None`` when the table carries no frame data. The digest is over the base64
    strings themselves, so it is exact and cheap - it never decodes a sample.
    """
    rows = keyframes(table)
    if not rows:
        return None
    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row.get("position")).encode())
        digest.update(b"\x00")
        data = row.get("wave_data")
        digest.update((data if isinstance(data, str) else "").encode())
        digest.update(b"\x1f")
    return digest.hexdigest()[:20]


def payload_bytes(table: Any) -> int:
    """The serialized size of a table value, as the chunk would carry it."""
    try:
        return len(json.dumps(table, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return -1


def describe(table: Any, *, oscillator: int | None = None) -> dict[str, Any]:
    """A compact description of a table: what a caller may act on.

    Deliberately **no wave data**: the caller learns how many frames the table has,
    where they sit, which components are present, and its content id. The samples
    are the plugin's business.
    """
    comps = components(table)
    comp = source_component(table)
    rows = keyframes(table)
    positions = frame_positions(table)
    out: dict[str, Any] = {
        "oscillator": oscillator,
        "name": table.get("name") if isinstance(table, dict) else None,
        "table_id": table_id(table),
        "n_components": len(comps),
        "component_types": [comp_["type"] for comp_ in comps],
        "n_frames": len(rows),
        "frame_positions": positions,
        "frame_axis_max": FRAME_AXIS_MAX,
        "n_samples_per_frame": sample_count(table),
        "total_samples": total_samples(table),
        "payload_bytes": payload_bytes(table),
        "content_bytes": content_bytes(table),
        "is_multi_frame": len(rows) >= 2,
        "has_wave_source": comp is not None,
    }
    if not rows:
        out["why_no_frames"] = (
            "no 'Wave Source' component with keyframes: the table's components are "
            "processing stages only, or the slot is empty"
        )
    elif len(rows) == 1:
        out["why_this_matters"] = (
            "a SINGLE-frame table is what makes `Oscillator N Wave Frame` inert: there "
            "is no second frame to move to, so a write or a modulation route to the "
            "frame returns the table's own frame"
        )
    return out


def describe_slots(document: dict[str, Any]) -> dict[str, Any]:
    """Every wavetable slot of a decoded document (``codec.decode`` view), described.

    The document view carries ``settings`` as a decoded mapping; the table list lives
    at ``settings.wavetables``. A document without the list is reported as such
    (``present: false``) rather than repaired - an absent list means the table channel
    is not where it is expected, which is exactly what a caller must not be told is fine.
    """
    settings = document.get("settings") if isinstance(document, dict) else None
    settings = settings if isinstance(settings, dict) else {}
    tables = settings.get(SETTINGS_KEY)
    if not isinstance(tables, list):
        return {
            "present": False,
            "n_slots": 0,
            "slots": [],
            "note": (
                "the document has no 'settings.wavetables' list: wavetable selection is "
                "document state, and without the list there is nowhere for it to live"
            ),
        }
    slots: list[dict[str, Any]] = []
    for index, table in enumerate(tables):
        oscillator = index + 1 if (index + 1) in SLOTS else None
        slots.append(describe(table, oscillator=oscillator))
    return {
        "present": True,
        "n_slots": len(tables),
        "known_slots": list(SLOTS),
        "slots": slots,
        "note": "a table is identified by `table_id` (a digest of its frame data), not by `name`",
    }


# ---------------------------------------------------------------------------
# The safety rule (relative; see HOST_ABORT_EVIDENCE)
# ---------------------------------------------------------------------------


def check_writable(value: Any, *, current: Any, live_version: str | None) -> dict[str, Any]:
    """REFUSE a table whose write is not proven safe on this build.

    The rule is RELATIVE when the current table is known, and it refuses growth in the
    dimensions the measurement names:

    - **frames** - the 8-frame payload was the one that did not land and once aborted
      the host;
    - **total samples** - the buffer the hypothesis says the deserialiser sizes from
      what the engine already holds;
    - **content size** (the serialized size with `name`/`author`/`version` blanked) - a
      label may get longer (names are cosmetic: the live instance holds three tables all
      called "Init"), the wave-carrying graph may not.

    The raw serialized size is additionally capped at the one measured-safe number, so a
    payload that grows by a *document-sized* amount cannot ride in on a label. Returns a
    verdict dict; raises :class:`InvalidValue` when refusing, carrying the measurement
    that produced the bound.
    """
    if not isinstance(value, dict):
        raise InvalidValue(
            "a wavetable slot must be written as a table object, not "
            f"{type(value).__name__}",
            detail={"accepted": "the document's own table shape (a JSON object with groups/components)"},
        )
    verdict: dict[str, Any] = {
        "n_frames": len(keyframes(value)),
        "total_samples": total_samples(value),
        "payload_bytes": payload_bytes(value),
        "content_bytes": content_bytes(value),
        "has_wave_source": source_component(value) is not None,
        "table_id": table_id(value),
        "verified_payload_bytes": VERIFIED_PAYLOAD_BYTES,
        "current": describe(current) if isinstance(current, dict) else None,
        "risk": (
            "writing a table larger than the engine holds did not land, and a growing "
            "payload with a version that disagreed with the plugin's aborted reaper.exe "
            "once (ucrtbase.dll, 0xc0000409 STATUS_STACK_BUFFER_OVERRUN)"
        ),
        "hypothesis": HOST_ABORT_EVIDENCE["hypothesis"],
        "the_settled_experiment": HOST_ABORT_EVIDENCE["the_settled_experiment"],
    }
    if source_component(value) is None or not keyframes(value):
        raise InvalidValue(
            "the supplied table carries no 'Wave Source' frames, so it stores no wave "
            "data; the plugin accepts such a slot and it sounds like silence",
            detail={**verdict, "component_types": [c.get("type") for c in components(value)]},
        )
    if not live_version:
        raise InvalidValue(
            "the live document carries no `synth_version`, so a table written into it "
            "cannot be verified: the plugin restamps `version` on re-serialisation "
            "(measured), and without knowing what it will write, a landed write would be "
            "reported as a failure",
            detail={**verdict, "why": "version_stamping_impossible"},
        )
    if isinstance(current, dict) and source_component(current) is not None:
        current_frames = len(keyframes(current))
        current_samples = total_samples(current)
        current_bytes = payload_bytes(current)
        current_content = content_bytes(current)
        cap = max(VERIFIED_PAYLOAD_BYTES, current_bytes)
        verdict["growth_in_frames"] = verdict["n_frames"] - current_frames
        verdict["growth_in_samples"] = verdict["total_samples"] - current_samples
        verdict["growth_in_content_bytes"] = verdict["content_bytes"] - current_content
        verdict["growth_in_bytes"] = verdict["payload_bytes"] - current_bytes
        verdict["payload_cap_bytes"] = cap
        growing = (
            verdict["n_frames"] > current_frames
            or verdict["total_samples"] > current_samples
            or verdict["content_bytes"] > current_content
            or verdict["payload_bytes"] > cap
        )
        if growing:
            raise InvalidValue(
                f"refusing to write a {verdict['n_frames']}-frame / "
                f"{verdict['total_samples']}-sample table over the {current_frames}-frame / "
                f"{current_samples}-sample one the engine holds: a growing payload cannot land "
                "on this build, and a mismatched version on a growing payload aborted the host once",
                detail={
                    **verdict,
                    "rule": "refuse a table that GROWS beyond what the engine holds (frames, "
                            "total samples, wave-carrying content bytes; the label fields are free)",
                    "why_refuse": (
                        "the write CANNOT succeed (measured: the plugin's own read-back keeps the "
                        "old table), and the one host abort this channel produced lived in exactly "
                        "that combination. A writer that cannot succeed and once aborted the host "
                        "must not be able to try by default."
                    ),
                    "safer_alternative": HOST_ABORT_EVIDENCE["the_settled_experiment"],
                },
            )
        verdict["rule"] = "non-growing: allowed (measured to land and verify)"
    else:
        verdict["rule"] = (
            "no current table to compare against: the absolute bound is applied "
            f"({VERIFIED_PAYLOAD_BYTES} bytes)"
        )
        if verdict["payload_bytes"] > VERIFIED_PAYLOAD_BYTES:
            raise InvalidValue(
                f"refusing to write a {verdict['payload_bytes']}-byte table into a slot whose "
                "current content is unknown: it exceeds the only size measured safe on this build",
                detail={**verdict, "max_verified_payload_bytes": VERIFIED_PAYLOAD_BYTES},
            )
    return {"ok": True, **verdict}


def stamp_version(table: dict[str, Any], live_version: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(value, note)`` with ``version`` stamped to the live plugin's.

    MEASURED: the plugin restamps `version` on re-serialisation, so a value carrying a
    preset's own version (e.g. ``"1.0.7"``) makes the exact-equality read-back report a
    LANDED write as a failure - and the rollback then puts the old table back. The
    stamp is not a guess: it is what the plugin writes, observed.
    """
    out = copy.deepcopy(table)
    if isinstance(out, dict) and live_version:
        before = out.get("version")
        out["version"] = live_version
        return out, {"stamped_version_from": before, "stamped_version_to": live_version}
    return out, {}


def live_version(document: Any) -> str | None:
    """The plugin's own ``synth_version``, from the document's TOP level.

    Not from ``settings``: the top-level key is the one that carries it (``'1.5.5'``)
    while ``settings.synth_version`` is absent.
    """
    if not isinstance(document, dict):
        return None
    value = document.get("synth_version")
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Tables on disk (.vital preset files are the SAME document)
# ---------------------------------------------------------------------------


def read_preset_tables(path: str) -> list[dict[str, Any]] | None:
    """Every wavetable slot a ``.vital`` preset carries, or ``None`` if unusable.

    ``None`` means "this file is not a Vital document", which is an ordinary outcome
    when walking a folder. A ``.vital`` file may carry a small binary header, so the
    document starts at the first ``{"author`` marker (the parser the POC used).
    """
    if not os.path.exists(path):
        return None
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    index = raw.find(b'{"author')
    if index < 0:
        return None
    try:
        document = json.JSONDecoder().raw_decode(raw[index:].decode("utf-8", "replace"))[0]
    except Exception:  # noqa: BLE001 - an unreadable preset is an ordinary outcome
        return None
    tables = ((document.get("settings") or {}).get(SETTINGS_KEY)) if isinstance(document, dict) else None
    if not isinstance(tables, list):
        return None
    return [copy.deepcopy(row) for row in tables if isinstance(row, dict)]


def table_from_preset(path: str, oscillator: Any) -> dict[str, Any]:
    """The wavetable one ``.vital`` preset carries for an oscillator.

    Returns a **deep copy**: the caller cannot mutate a parsed file into the live
    document. The file's own ``version`` is left as it is - :func:`stamp_version`
    replaces it with the live plugin's before any write.
    """
    slot = check_slot(oscillator)
    tables = read_preset_tables(path)
    if tables is None:
        if not os.path.exists(path):
            raise InvalidValue(f"no preset file at {path!r}", detail={"path": path})
        raise Unreadable(
            f"{path!r} does not contain a usable Vital document",
            detail={
                "path": path,
                "looked_for": 'the {"author marker and a settings.wavetables list',
                "why": "a .vital file is a JSON document after a small binary header; without "
                       "the marker or the list there is nothing to read",
            },
        )
    if len(tables) < slot:
        raise InvalidValue(
            f"{path!r} carries {len(tables)} wavetable slot(s); oscillator {slot} is not addressable",
            detail={"path": path, "slots": list(SLOTS)},
        )
    return tables[slot - 1]


def read_slot_from_preset(
    path: str, *, oscillator: Any, preset_slot: Any = None, expected_table_id: str | None = None
) -> dict[str, Any]:
    """A table taken from a preset file, with its provenance, ready for the guard.

    ``preset_slot`` defaults to the same number as ``oscillator`` (a preset that
    carries three tables lines them up with the three oscillators). ``expected_table_id``
    is a *content* check: a mismatch means the file changed or the wrong slot was named,
    which must refuse rather than write a table nobody chose.
    """
    slot = check_slot(oscillator)
    source_slot = check_slot(preset_slot) if preset_slot is not None else slot
    table = table_from_preset(path, source_slot)
    descriptor = describe(table, oscillator=slot)
    if expected_table_id and descriptor["table_id"] != expected_table_id:
        raise InvalidValue(
            "the preset's table does not match the requested table_id",
            detail={
                "requested": expected_table_id,
                "found": descriptor["table_id"],
                "why": "the id is a content digest; a mismatch means the file changed or the "
                       "wrong slot was named",
            },
        )
    return {
        "value": table,
        "descriptor": descriptor,
        "source": f"{os.path.basename(path)} oscillator {source_slot}",
        "preset": path,
        "preset_slot": source_slot,
    }
