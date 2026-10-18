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

THE THREE RULES THIS MODULE EXISTS FOR
-------------------------------------
1. **A table is identified by its CONTENT, never by its ``name``.** The live
   instance holds three tables all labelled ``"Init"`` while 112 distinct
   single-frame tables in the preset corpus carry that same name - a label is not
   an identity, a digest is.
2. 🔴 **A table that GROWS the document needs ROOM first** (P39, 2026-10-17).
   P30 (2026-09-29) measured the symptom: a single-frame replacement (11,159 ->
   11,149 bytes) **committed and verified** live, while an 8-frame / 87,855-byte
   payload did **not land at all** and, with a `version` that disagreed with the
   plugin's, **aborted `reaper.exe`** (faulting module `ucrtbase.dll`, exception
   `0xc0000409` = `STATUS_STACK_BUFFER_OVERRUN`). P39 measured the cause: a state
   write lands iff the new document is at most the ~32 characters that follow its
   JSON inside the plugin's state longer than the one the host holds - a FRAMING
   bound, true of any value, and item B's own size fields do not move it. So the
   table is delivered in TWO writes: a **balloon** (the same slot inflated with
   stub keyframes, which the plugin re-serializes as full frames, so the state
   becomes bigger than the table) and then the table itself as a **shrink**, which
   always lands. Measured end to end with a 16-frame / 175,497-byte table.
   `version` is still stamped from the live document.
3. **The plugin re-serializes every keyframe as a FULL frame**
   (`WaveSourceKeyframe::jsonToState` decodes into a frame-sized block and memcpys
   all of it). That is what makes rule 2 work, and it is why a table written with
   short payloads is not comparable by equality.

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
import math
import os
import struct
from typing import Any

from ...errors import InvalidValue, Unreadable

__all__ = [
    "SLOTS",
    "SETTINGS_KEY",
    "SOURCE_COMPONENT",
    "SOURCE_COMPONENTS",
    "AUDIO_SOURCE_COMPONENT",
    "AUDIO_REQUANT_STEP",
    "FRAME_AXIS_MAX",
    "FRAME_SAMPLES",
    "STUB_SAMPLES",
    "full_frame_chars",
    "frame_stub",
    "balloon_component",
    "balloon_table",
    "balloon_sizing",
    "HOST_ABORT_EVIDENCE",
    "VERIFIED_PAYLOAD_BYTES",
    "path_for",
    "check_slot",
    "parse_wavetable_path",
    "components",
    "source_component",
    "carriers",
    "carrier",
    "component_keyframes",
    "decode_frame",
    "audio_payload",
    "audio_samples",
    "carries_waves",
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

#: The component type that carries an audible table as KEYFRAMES. The other measured types are
#: processing stages in the table's own graph (`Wave Folder`, `Frequency Filter`, `Phase Shift`,
#: `Wave Window`, `Wave Warp`) - but NOT all of them: see `SOURCE_COMPONENTS`.
SOURCE_COMPONENT = "Wave Source"

#: **Every** component type that CARRIES wave data, from the plugin's own factory
#: (`common/wavetable/wavetable_component_factory.cpp`: ten types, four of them sources). A slot
#: whose graph holds only modifiers stores no wave data and renders silence; a slot whose graph
#: holds one of these does.
#:
#: * ``Wave Source`` - `keyframes[].wave_data` (2048 float32 samples per frame),
#: * ``Audio File Source`` - ONE `audio_file` (base64 int16 PCM) plus windows into it; measured
#:   over the preset library: the growl/dubstep family is built on these (449 of 536
#:   dubstep/growl-ish presets carry a custom table, median payload 164,353 B, and most of those
#:   are audio-file slots of up to ~1.4 MB of state),
#: * ``Line Source`` - a line generator's points rendered into frames,
#: * ``Shepard Tone Source``.
#:
#: P39: the guard used to accept ONLY `Wave Source`, so every growl preset lost exactly the part
#: that makes it growl.
SOURCE_COMPONENTS: tuple[str, ...] = (
    "Wave Source",
    "Audio File Source",
    "Line Source",
    "Shepard Tone Source",
)

#: The audio-file source: its wave data is one base64 int16 PCM string.
AUDIO_SOURCE_COMPONENT = "Audio File Source"

#: How far an ``audio_file`` payload may move on the plugin's round trip, in PCM steps.
#: MEASURED (P39e, 2026-10-17, live 1.5.5, twice on the same 1,409,808-byte table): the plugin
#: converts int16 -> float on load and float -> int16 on save, and 8,856 of 528,388 samples
#: (1.676 %) came back moved by EXACTLY one step - the same class as the sample block's measured
#: requantization, and equally unavoidable. A length change is structural and never accepted.
AUDIO_REQUANT_STEP = 1

#: The frame axis is 0..256 in every table measured (`kNumOscillatorWaveFrames`).
FRAME_AXIS_MAX = 256

#: Samples in one frame (`WaveFrame::kWaveformSize`). Measured twice: the P30 inventory
#: decoded the INIT table's single frame as 2,048 samples, and every corpus frame decoded
#: here is 2,048 samples wide.
FRAME_SAMPLES = 2048

#: The serialized size (characters, not bytes) of the base64 payload one FULL frame carries.
#: This is the number the plugin re-serializes per keyframe, whatever was written.
def full_frame_chars() -> int:
    return len(base64.b64encode(b"\x00" * (FRAME_SAMPLES * 4)).decode("ascii"))


#: The payload measured safe BEFORE the mechanism was known (P30): the INIT table a fresh
#: instance holds (1 frame, 2,048 samples, 11,159 serialized bytes). P39 replaced this cap
#: as a RULE (see `HOST_ABORT_EVIDENCE`) - it is kept as the recorded measurement a doc or a
#: test cites, and the guard no longer refuses anything larger.
VERIFIED_PAYLOAD_BYTES = 11_159

#: What P30 saw, what P39 measured, and which of the two is the rule.
HOST_ABORT_EVIDENCE: dict[str, Any] = {
    "established": [
        "a non-growing single-frame replacement LANDS: 11,159 -> 11,149 -> 11,159 bytes, "
        "committed and read-back verified through the state chunk (P30)",
        "a growing payload does NOT land: stage verify, the plugin's own read-back is "
        "the unchanged table, 0 controls moved (P30)",
        "a growing payload whose `version` disagreed with the plugin's ABORTED reaper.exe "
        "once (ucrtbase.dll, 0xc0000409 STATUS_STACK_BUFFER_OVERRUN) (P30)",
        "the plugin restamps `version` on re-serialisation, so a value not stamped with "
        "the live version cannot be verified (P30, still true)",
        "P39 (2026-10-17) measured WHAT the growing write hit: a state write lands iff "
        "`json_end_new <= len(prev item B) - 8`, i.e. the document may grow by at most the "
        "32 characters that follow its JSON inside the plugin's state - measured on plain "
        "string fields, with the block delta held constant (32 lands, 33 does not). It is "
        "a framing bound, not a wavetable bound.",
        "P39 then landed a 16-frame / 175,497-byte table in TWO writes: an inflated table "
        "first (many keyframes with tiny payloads, which the plugin re-serializes as full "
        "frames - `WaveSourceKeyframe::jsonToState` memcpys a whole frame from a buffer "
        "sized for one), then the real table as a shrink. Read back frame-for-frame with "
        "`table_id` equality, no NaN, and the document equal to the capture except the slot.",
    ],
    "hypothesis": (
        "the plugin's state deserialiser sizes its wavetable buffer from what the engine "
        "ALREADY holds, so a growing payload cannot be stored - and, with a mismatched "
        "`version`, could not be parsed safely either"
    ),
    "superseded_by": (
        "P39: the observed refusal is the host handing the plugin "
        "`new_state[0 : len_prev - 8]`, so the document could only grow by the 32 characters "
        "that follow its JSON. A table that grows the state therefore needs ROOM first - the "
        "`balloon` step below - after which the same table lands. The buffer hypothesis is "
        "not needed to explain any measurement, and the P30 abort is the same truncation "
        "cutting the JSON, not a size limit of the wavetable engine."
    ),
    "not_separated": ["total size", "frame count", "the interpolation value"],
    "why_not_separated_known": (
        "P39 separated them for the STATE channel (any value, any size, no wavetable involved: "
        "the bound is 32 characters of document growth per write). What remains unmeasured is "
        "whether a table can be delivered by a route the host does not bound at all - untested "
        "and not needed: the balloon path is measured end to end."
    ),
    "provenance": "POC measurement 2026-09-29 (P30) + live measurement 2026-10-17 (P39), "
                  "evidence under %LOCALAPPDATA%\\reaper_reapy_mcp\\run_artifacts\\*p39*",
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
    """The `Wave Source` component (the keyframe-carrying one), or ``None``.

    A table whose components are all processing stages has no wave data to read; that is
    reported as such rather than treated as a single-frame table. Use :func:`carrier` when the
    question is "where does this table's wave data live", whatever its type.
    """
    for comp in _first_group_components(table):
        if comp.get("type") == SOURCE_COMPONENT:
            return comp
    return None


def carriers(table: Any) -> list[dict[str, Any]]:
    """Every component of the first group that CARRIES wave data, in graph order.

    The order matters: a table's graph renders left to right, so the last source wins. The
    balloon uses that (its stub is a source, placed where the real one was).
    """
    return [comp for comp in _first_group_components(table) if comp.get("type") in SOURCE_COMPONENTS]


def carrier(table: Any) -> dict[str, Any] | None:
    """The FIRST wave-carrying component of the table (any source type), or ``None``."""
    rows = carriers(table)
    return rows[0] if rows else None


def component_keyframes(component: Any) -> list[dict[str, Any]]:
    """The keyframes of ONE component (any type: they all carry `position`)."""
    if not isinstance(component, dict):
        return []
    raw = component.get("keyframes") or []
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def audio_payload(table: Any) -> str | None:
    """The ``audio_file`` string an audio-file slot carries, or ``None``."""
    for comp in carriers(table):
        if comp.get("type") != AUDIO_SOURCE_COMPONENT:
            continue
        value = comp.get("audio_file")
        if isinstance(value, str) and value:
            return value
    return None


def audio_samples(table: Any) -> int:
    """PCM samples in an audio-file slot's payload (int16, so decoded bytes / 2).

    Decoding is bounded to the base64 header arithmetic only - the payload is never unpacked
    into an array here, because callers ask for this on a 1.4 MB string.
    """
    value = audio_payload(table)
    if not value:
        return 0
    try:
        return len(base64.b64decode(value + "=" * (-len(value) % 4))) // 2
    except Exception:  # noqa: BLE001 - a malformed payload is an ordinary outcome
        return 0


def carries_waves(table: Any) -> bool:
    """Does the table carry wave data at all?

    A graph of modifiers only (no source component) renders silence, which is why a write of
    such a table is refused: the plugin accepts it and the oscillator goes quiet. Everything
    else is wave data - the four source types carry it in four different shapes.
    """
    for comp in carriers(table):
        kind = comp.get("type")
        if kind == SOURCE_COMPONENT and component_keyframes(comp):
            return True
        if kind == AUDIO_SOURCE_COMPONENT and audio_payload(table):
            return True
        if kind in ("Line Source", "Shepard Tone Source"):
            # Measured: a `Line Source` renders from its generator (points/powers) and a
            # Shepard source from its own fields; both are sources, so a present component of
            # either type IS the wave data. Their payload is not decoded here.
            return True
    return False


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


def decode_frame(table: Any, index: int = 0) -> list[float] | None:
    """The samples of one keyframe of the table, decoded.

    ``None`` when the index is out of range or the payload cannot be decoded - a caller reading a
    frame back has to see "this frame carries no usable data" rather than an empty list that looks
    like silence. The plugin stores one frame per keyframe (measured), so ``index`` is the frame.
    """
    rows = keyframes(table)
    if not 0 <= int(index) < len(rows):
        return None
    decoded = _decode_wave_data(rows[int(index)].get("wave_data"))
    if decoded is None:
        return None
    return list(decoded[1])


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
    """A CONTENT identity for a table: a digest of everything that carries wave data.

    ``None`` when the table carries none. The digest is over the base64 strings themselves, so
    it is exact and cheap - it never decodes a sample.

    **The keyframe digest is byte-for-byte the one P30 shipped** (positions and `wave_data`
    with the same separators), so every id recorded in the journal since then stays valid. The
    payloads of the OTHER source types (an audio file, a line generator, a Shepard source) are
    appended as their own sections, which is what gives an audio-file table an identity at all -
    without it an audio slot could not be verified or recognised.

    Like the P30 digest, this is the **wave content and nothing else**: a label, a version, a
    modifier or a graph change is not an identity (the corpus lesson: the live instance held
    three tables all called "Init", and 112 corpus tables carried that same name). The
    verification compares the graph field-wise besides the id.
    """
    rows = keyframes(table)
    extra = [comp for comp in carriers(table) if comp.get("type") != SOURCE_COMPONENT]
    if not rows and not extra:
        return None
    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row.get("position")).encode())
        digest.update(b"\x00")
        data = row.get("wave_data")
        digest.update((data if isinstance(data, str) else "").encode())
        digest.update(b"\x1f")
    for comp in extra:
        digest.update(str(comp.get("type")).encode())
        digest.update(b"\x1e")
        if comp.get("type") == AUDIO_SOURCE_COMPONENT:
            digest.update(str(comp.get("audio_file") or "").encode())
        else:
            digest.update(
                json.dumps(comp, sort_keys=True, separators=(",", ":"), default=str).encode()
            )
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
    audio_chars = len(audio_payload(table) or "")
    out: dict[str, Any] = {
        "oscillator": oscillator,
        "name": table.get("name") if isinstance(table, dict) else None,
        "table_id": table_id(table),
        "n_components": len(comps),
        "component_types": [comp_["type"] for comp_ in comps],
        "source_types": [comp_["type"] for comp_ in carriers(table)],
        "n_frames": len(rows),
        "frame_positions": positions,
        "frame_axis_max": FRAME_AXIS_MAX,
        "n_samples_per_frame": sample_count(table),
        "total_samples": total_samples(table),
        "payload_bytes": payload_bytes(table),
        "content_bytes": content_bytes(table),
        "is_multi_frame": len(rows) >= 2,
        "has_wave_source": comp is not None,
        "carries_waves": carries_waves(table),
    }
    if audio_chars:
        out["audio_chars"] = audio_chars
        out["audio_samples"] = audio_samples(table)
        out["audio_source"] = AUDIO_SOURCE_COMPONENT
        out["why_no_frames"] = (
            "this slot's wave data is an AUDIO FILE, not keyframes: its table is built by "
            "reading windows out of the payload measured by `audio_chars`/`audio_samples`"
        )
    if not rows and not out.get("why_no_frames"):
        out["why_no_frames"] = (
            "no source component carrying wave data in the first group: the table's components "
            "are processing stages only, so it renders silence"
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


#: Samples one balloon keyframe carries for real. Everything past them is the frame buffer
#: the plugin preallocated (`MemoryOutputStream(sizeof(float) * kWaveformSize)`) - measured
#: finite on the live build, and only ever visible inside the slot being replaced.
STUB_SAMPLES = 8


def frame_stub(table: Any, *, samples: int = STUB_SAMPLES) -> str:
    """A short base64 payload taken from the table's own opening samples.

    The balloon's keyframes carry this, so the part that is written is the table's OWN data
    rather than an invented waveform: the first samples of its first keyframe, or - for an
    `Audio File Source` slot, which has no keyframes - the first samples of its audio payload.
    A table whose data cannot be read falls back to silence.
    """
    silence = base64.b64encode(b"\x00" * (4 * max(1, int(samples)))).decode("ascii")
    rows = keyframes(table)
    if rows:
        decoded = _decode_wave_data(rows[0].get("wave_data"))
        if decoded is not None:
            _count, values = decoded
            if values:
                take = values[: int(samples)]
                return base64.b64encode(struct.pack("<%df" % len(take), *take)).decode("ascii")
        return silence
    audio = audio_payload(table)
    if audio:
        # Only the head of the payload is decoded: the base64 prefix is sliced at a group
        # boundary (4 characters = 3 bytes), so a 1.4 MB string costs a few microseconds here.
        wanted_bytes = 2 * max(1, int(samples))
        head = audio[: 4 * ((wanted_bytes + 2) // 3)]
        try:
            raw = base64.b64decode(head + "=" * (-len(head) % 4))
        except Exception:  # noqa: BLE001 - a malformed payload falls back to silence
            return silence
        count = len(raw) // 2
        if count:
            pcm = struct.unpack("<%dh" % count, raw[: count * 2])
            values = [value / 32768.0 for value in pcm[: int(samples)]]
            return base64.b64encode(struct.pack("<%df" % len(values), *values)).decode("ascii")
    return silence


def balloon_component(frames: int, stub: str) -> dict[str, Any]:
    """The stub component a balloon puts where the real source was.

    A `Wave Source` with ``frames`` keyframes that all carry the same short payload. It is
    what makes the balloon work for EVERY table type: the plugin re-serializes one FULL frame
    per keyframe whatever was written (`WaveSourceKeyframe::jsonToState`), so this component
    turns a ~200-byte write into ``frames x 10,924`` characters of state.
    """
    count = max(1, int(frames))
    rows: list[dict[str, Any]] = []
    for index in range(count):
        rows.append(
            {
                "position": int(round(index * FRAME_AXIS_MAX / count)),
                "wave_data": stub,
            }
        )
    return {
        "type": SOURCE_COMPONENT,
        "interpolation": 1,
        "interpolation_style": 1,
        "keyframes": rows,
    }


def balloon_table(current: Any, target: Any = None, *, frames: int, stub: str | None = None) -> dict[str, Any]:
    """The table a balloon writes: the LIVE table with its source replaced by stub keyframes.

    Built from the live table - not the target - so the write differs from what the engine
    already holds in its source alone, which is the smallest change that still re-serializes
    large; the graph's MODIFIERS (folder, filter, warp, window) are carried over as they are.

    P39d (live): the component the stub replaces is the FIRST wave-carrying component of ANY
    type. Measured on an `Audio File Source` slot (a growl table: 1,409,808 bytes of audio),
    replacing it made the write a shrink and the read-back `frames x 10,924` characters, which
    is the room the table itself then needed. A slot with no source component gets the stub
    inserted at the head of the graph instead.

    ``stub`` is the payload every keyframe carries (`frame_stub(target)` by default, i.e. the
    opening samples of the table being delivered), so a caller can pass the exact string it
    sized the balloon with - the write the codec makes is then the one that was measured.

    The table renders the STUB while it is live (a few seconds between the two writes), so the
    slot is near-silent in that window rather than wrong: it is the slot being replaced anyway.
    """
    if not isinstance(current, dict):
        raise InvalidValue(
            "a balloon needs the table the engine holds (it is the table it inflates)",
            detail={"type": type(current).__name__},
        )
    table = copy.deepcopy(current)
    groups = table.get("groups")
    if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
        raise InvalidValue(
            "the live table has no group to inflate: a balloon needs the table the engine "
            "holds, and this value has no `groups[0]` to put a source into",
            detail={"payload_bytes": payload_bytes(current)},
        )
    group = groups[0]
    comps = list(group.get("components") or [])
    payload = stub if isinstance(stub, str) and stub else frame_stub(target)
    replacement = balloon_component(frames, payload)
    for index, comp in enumerate(comps):
        if isinstance(comp, dict) and comp.get("type") in SOURCE_COMPONENTS:
            comps[index] = replacement
            break
    else:
        comps.insert(0, replacement)
    group["components"] = comps
    return table


def balloon_sizing(
    target: Any,
    current: Any,
    *,
    budget_chars: int,
    stub: str | None = None,
    min_growth_chars: int = 0,
    want_room_chars: int | None = None,
) -> dict[str, Any]:
    """How many stub keyframes the balloon needs, and whether that write can land.

    Two conditions, both measured (P39):

    * **the room must be enough** - the plugin re-serializes one FULL frame per keyframe
      (`WaveSourceKeyframe::jsonToState` decodes into a frame-sized block and memcpys all of
      it), so `frames x full_frame_chars()` has to cover what the next write needs: by default
      the target table's own serialized size, or `want_room_chars` when the caller is opening
      room for a DIFFERENT channel (the sample block, whose growth may exceed a single write -
      the room comes from a wavetable slot precisely because that balloon is a shrink of the
      live table while its read-back is large);
    * **the balloon itself must land** - its size may exceed the live table's by at most
      `budget_chars` (the state channel's measured landing bound), and it is the live table's
      own source that pays for the stub rows, so a table that is already large affords more
      stub keyframes than one that is nearly empty.

    Never raises: an impossible combination is returned with ``ok: false`` and the numbers, so
    the caller names it as a gap rather than guessing a smaller balloon.
    """
    payload = stub if isinstance(stub, str) and stub else frame_stub(target)
    frame_chars = max(full_frame_chars(), len(payload), 1)
    target_chars = max(payload_bytes(target), 0)
    current_chars = payload_bytes(current) if isinstance(current, dict) else 0
    #: The component the balloon REPLACES is the first wave-carrying one, whatever its type: an
    #: audio-file source is dropped whole (which is what pays for the write), and a `Wave Source`
    #: contributes its keyframes, which the stub rows replace. Everything else in the graph (the
    #: modifiers) is KEPT, so it has to be counted as part of the room.
    replaced = carrier(current) if isinstance(current, dict) else None
    replaced_chars = payload_bytes(replaced) if isinstance(replaced, dict) else 0
    try:
        probe = balloon_table(current, frames=1, stub=payload)
        base_chars = max(0, payload_bytes(probe) - len(payload))
    except InvalidValue:
        return {
            "frames": 0,
            "frame_chars": frame_chars,
            "target_chars": target_chars,
            "room_chars": 0,
            "write_growth_chars": None,
            "budget_chars": budget_chars,
            "live_table_chars": current_chars,
            "live_keyframe_chars": replaced_chars,
            "replaced_chars": replaced_chars,
            "stub_chars": len(payload),
            "ok": False,
            "why": (
                "the slot the engine holds cannot be inflated at all: it has no `groups[0]` to "
                "put a stub source into, so there is nowhere for the room to come from"
            ),
        }
    #: The second write may grow by the channel's landing bound, so the balloon only has to
    #: cover the need minus that - and one full frame per keyframe buys `frame_chars` of it.
    if want_room_chars is None:
        want_room = max(0, target_chars - max(0, int(budget_chars))) + max(0, int(min_growth_chars))
    else:
        want_room = max(0, int(want_room_chars))
    frames = max(1, int(math.ceil(want_room / frame_chars)))
    if frames > FRAME_AXIS_MAX:
        return {
            "frames": frames,
            "frame_chars": frame_chars,
            "target_chars": target_chars,
            "room_chars": frames * frame_chars,
            "write_growth_chars": None,
            "budget_chars": budget_chars,
            "live_table_chars": current_chars,
            "live_keyframe_chars": replaced_chars,
            "replaced_chars": replaced_chars,
            "stub_chars": len(payload),
            "ok": False,
            "why": (
                f"this table needs {frames} stub keyframes to make room, which is more than the "
                f"{FRAME_AXIS_MAX}-frame axis this build measures"
            ),
        }
    built = balloon_table(current, frames=frames, stub=payload)
    growth = payload_bytes(built) - current_chars
    room = base_chars + frames * frame_chars
    next_growth = target_chars - room
    return {
        "frames": frames,
        "frame_chars": frame_chars,
        "target_chars": target_chars,
        "room_chars": room,
        "want_room_chars": want_room,
        "predicted_readback_chars": room,
        "next_write_growth_chars": next_growth,
        "write_growth_chars": growth,
        "budget_chars": budget_chars,
        "live_table_chars": current_chars,
        "live_keyframe_chars": replaced_chars,
        "replaced_chars": replaced_chars,
        "balloon_chars": payload_bytes(built),
        "stub_chars": len(payload),
        "ok": growth <= budget_chars and next_growth <= budget_chars,
    }


def check_writable(value: Any, *, current: Any, live_version: str | None) -> dict[str, Any]:
    """Judge a whole-table write: is it deliverable, and what does delivery cost?

    The P30 refusal this replaces was a **cap on the table's own size** (frames, total
    samples, wave-carrying content bytes, and the one payload measured safe). P39 measured
    what a growing state write actually hits - the host hands the plugin the state sliced at
    its own remembered length, so ANY value may grow the document by at most the ~32
    characters that follow its JSON - and then landed a 16-frame / 175,497-byte table by
    opening room first (see :func:`balloon_sizing`). A size cap is therefore no longer the
    rule, and this guard no longer refuses one.

    What it still refuses, because no measurement supports it:

    * a value that is not a table object, or carries NO wave data at all (a graph of modifiers
      only - the plugin accepts such a slot and it sounds like silence). P39: the wave data may
      live in any of `SOURCE_COMPONENTS` - the keyframe `Wave Source`, an `Audio File Source`
      (how the growl/dubstep family is built), a `Line Source` or a `Shepard Tone Source` -
      and the version of this guard that knew only `Wave Source` refused every growl preset;
    * a write with no live ``synth_version`` to stamp, because the plugin restamps `version`
      on re-serialisation (measured) and an unstamped value cannot be verified.

    The returned verdict carries ``growth_in_bytes`` (the serialized delta against the live
    table) so the caller can decide whether the write needs a balloon.
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
        "source_types": [comp.get("type") for comp in carriers(value)],
        "has_wave_source": source_component(value) is not None,
        "carries_waves": carries_waves(value),
        "table_id": table_id(value),
        "verified_payload_bytes": VERIFIED_PAYLOAD_BYTES,
        "current": describe(current) if isinstance(current, dict) else None,
        "evidence": HOST_ABORT_EVIDENCE["superseded_by"],
    }
    if not carries_waves(value):
        raise InvalidValue(
            "the supplied table carries no wave data: its components are processing stages "
            "only (no `Wave Source`, `Audio File Source`, `Line Source` or `Shepard Tone "
            "Source` with content), and the plugin accepts such a slot as silence",
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
        verdict["growth_in_frames"] = verdict["n_frames"] - current_frames
        verdict["growth_in_samples"] = verdict["total_samples"] - current_samples
        verdict["growth_in_content_bytes"] = verdict["content_bytes"] - current_content
        verdict["growth_in_bytes"] = verdict["payload_bytes"] - current_bytes
        if verdict["growth_in_bytes"] > 0:
            verdict["rule"] = (
                "growing: needs room - the state channel writes a balloon for this slot first, "
                "then this table as a shrink (both writes measured to land, P39)"
            )
        else:
            verdict["rule"] = "non-growing: one write (measured to land, P30 and P39)"
    else:
        verdict["growth_in_bytes"] = None
        verdict["rule"] = (
            "no current table to compare against: the live slot decides whether room is needed"
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
