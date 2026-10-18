"""Vital wavetable CONSTRUCTION: the spec an MCP caller writes, and the table it becomes (P40).

WHY THIS MODULE EXISTS
----------------------
The layer could already put a table into an oscillator - but only a table that came from a preset
file (`plugin_presets`) or from the caller's own bytes (`plugin_write` with a raw document path).
This is the missing piece: a caller describes a waveform in the vocabulary of SOUND DESIGN, and the
layer builds the plugin's own table object from it, stamps the live version, and delivers it through
the measured state channel (the balloon, the verification, the rollback).

THE SPEC (one object, ONE source key - the whole protocol)
----------------------------------------------------------
    {"shape": "saw"}                                  one frame, the plugin's own Init shape
    {"shape": ["sine", "square"], "frames": 16}       a 16-frame morph (linear between items)
    {"shape": "pulse", "duty": 0.25}                  a pulse with a 25 % duty
    {"harmonics": [[1, 1.0], [3, 0.4], [5, 0.2]]}     additive: partial number, amplitude
    {"harmonics": [[[1, 1.0]], [[1, 1.0], [2, 0.5]]]} one spectrum PER FRAME (a morph)
    {"samples": [[0.0, 0.5, ...], [...]]}             raw samples; shortened rows are resampled
    {"name": "My Growl", "full_normalize": false, "remove_all_dc": false}   table-level options

RULES (each one is a refusal when broken, never a silent repair)
---------------------------------------------------------------
* exactly ONE of `shape` / `harmonics` / `samples`;
* `frames` (1..256) defaults to the number of items listed; a list of K items with `frames = N > K`
  is interpolated item by item, and the last item holds for the remaining frames (this is how a
  MORPH is specified: two shapes, sixteen frames);
* a frame is `2048` samples (`WaveFrame::kWaveformSize`, measured): a shorter `samples` row is
  RESAMPLED by linear interpolation - the plugin does not resample what it is given (measured: it
  memcpys a whole frame from a buffer sized for one, so a short payload is zero-padded), and a
  zero-padded row would be a different waveform from the one the caller described;
* values are clamped to [-1, 1]; NaN and infinity are refused;
* `shape` names are the vocabulary of :data:`SHAPES`; an unknown one is refused with the list.

WHAT THE PLUGIN DOES WITH IT (measured, and why the builder is shaped like this)
-------------------------------------------------------------------------------
* a frame is `wave_data`, base64 float32, exactly `2048` samples;
* the frame axis is 0..256, so a table of N frames places them at ``round(i * 256 / N)``;
* the plugin re-serializes every keyframe as a FULL frame whatever was written, restamps `version`
  with its own, and re-encodes the payloads - which is why the builder writes full frames and
  :func:`stamp_version` (in `wavetables`) replaces the version before the write.
"""

from __future__ import annotations

import base64
import math
import struct
from typing import Any, Iterable, Sequence

from ...errors import InvalidValue

__all__ = [
    "SHAPES",
    "MAX_FRAMES",
    "FRAME_SAMPLES",
    "SPEC_KEYS",
    "SOURCE_KEYS",
    "WavetableCapability",
    "shape_wave",
    "harmonics_wave",
    "spec_frames",
    "frame_summary",
    "build_table",
    "describe_spec",
    "preview",
]

#: The named single-cycle waveforms. Each is generated NAIVE (no band limiting) on purpose: the
#: plugin's own Init table is a naive saw - measured h2 = 0.500, h3 = 0.333, i.e. harmonics 1/n,
#: which is what a ramp with a discontinuity looks like before any band limiting.
SHAPES: tuple[str, ...] = ("sine", "triangle", "saw", "square", "pulse")

#: `kNumOscillatorWaveFrames` (measured): the frame axis is 0..256.
MAX_FRAMES = 256

#: `WaveFrame::kWaveformSize` (measured twice: the P30 inventory decoded the INIT table's single
#: frame as 2,048 samples and every corpus frame is 2,048 wide).
FRAME_SAMPLES = 2048

#: The one source key a spec must carry.
SOURCE_KEYS: tuple[str, ...] = ("shape", "harmonics", "samples")

#: Everything a spec may carry.
SPEC_KEYS: tuple[str, ...] = (
    *SOURCE_KEYS,
    "frames",
    "duty",
    "phase",
    "name",
    "author",
    "full_normalize",
    "remove_all_dc",
)


# ---------------------------------------------------------------------------
# Waveform generators
# ---------------------------------------------------------------------------


def shape_wave(
    name: str,
    *,
    samples: int = FRAME_SAMPLES,
    duty: float = 0.5,
    phase: float = 0.0,
) -> list[float]:
    """One cycle of a named shape, ``samples`` wide, peak 1.0.

    ``phase`` is in degrees (0..360), ``duty`` is the pulse width as a fraction of the cycle and is
    only read by ``pulse``. A name outside :data:`SHAPES` is refused with the list, so a typo can
    never become a table of silence.
    """
    key = str(name or "").strip().lower()
    if key not in SHAPES:
        raise InvalidValue(
            f"{name!r} is not a waveform this layer builds",
            detail={"shapes": list(SHAPES), "accepted": "a name from `shapes`"},
        )
    count = int(samples)
    if count < 2:
        raise InvalidValue(
            "a frame needs at least two samples",
            detail={"samples": samples, "minimum": 2},
        )
    width = float(duty)
    if key == "pulse" and not 0.0 < width < 1.0:
        raise InvalidValue(
            "a pulse's duty must be between 0 and 1",
            detail={
                "duty": duty,
                "accepted": "0 < duty < 1",
                # measured (2026-10-18): a caller reads `duty` as a percentage and passes 25 - the
                # unit is a fraction, so the refusal has to say which one it wants
                "note": "duty is a fraction of the cycle: 25 percent is 0.25",
            },
        )
    start = (float(phase) % 360.0) / 360.0
    out: list[float] = []
    for index in range(count):
        t = (start + index / count) % 1.0
        if key == "sine":
            value = math.sin(2.0 * math.pi * t)
        elif key == "triangle":
            value = 4.0 * abs(t - 0.5) - 1.0
        elif key == "saw":
            value = 2.0 * t - 1.0
        elif key == "square":
            value = 1.0 if t < 0.5 else -1.0
        else:  # pulse
            value = 1.0 if t < width else -1.0
        out.append(value)
    return out


def _parse_partials(partials: Any) -> list[tuple[float, float, float]]:
    """``[[k, amplitude], ...]`` or ``[[k, amplitude, phase_degrees], ...]``, validated."""
    if isinstance(partials, dict):
        rows: Iterable[Any] = [[key, value] for key, value in partials.items()]
    elif isinstance(partials, (list, tuple)):
        rows = partials
    else:
        raise InvalidValue(
            "`harmonics` must be a list of [partial, amplitude] rows (or one such list per frame)",
            detail={"got": type(partials).__name__},
        )
    parsed: list[tuple[float, float, float]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or not 1 <= len(row) <= 3:
            raise InvalidValue(
                "each harmonic is [partial, amplitude] or [partial, amplitude, phase_degrees]",
                detail={"row": row},
            )
        try:
            number = float(row[0])
            amplitude = float(row[1]) if len(row) > 1 else 1.0
            phase = float(row[2]) if len(row) > 2 else 0.0
        except (TypeError, ValueError) as exc:
            raise InvalidValue(
                "a harmonic's numbers must be numbers",
                detail={"row": row},
            ) from exc
        if number < 1 or number != int(number):
            raise InvalidValue(
                "a harmonic number is a whole number of cycles per frame, from 1 up",
                detail={"partial": row[0]},
            )
        if not _finite(amplitude) or not _finite(phase):
            raise InvalidValue("a harmonic's numbers must be finite", detail={"row": row})
        parsed.append((number, amplitude, phase))
    if not parsed:
        raise InvalidValue(
            "`harmonics` carries no partials, so the frame would be silence",
            detail={"accepted": "at least one [partial, amplitude] row"},
        )
    return parsed


def harmonics_wave(
    partials: Any, *, samples: int = FRAME_SAMPLES, normalize: bool = True
) -> list[float]:
    """One cycle built additively from ``(partial, amplitude, phase)`` rows.

    This is the sound-design primitive: a spectrum is a handful of numbers, and every classic shape
    is a spectrum too (a saw is ``1/n``, a square is odd ``1/n``). ``normalize`` scales the result
    to peak 1.0 - the SPEC path turns it on, because a frame that leaves [-1, 1] is a clipped frame
    and it is the ratios that carry the timbre; the raw sum stays available by passing False.
    """
    rows = _parse_partials(partials)
    count = int(samples)
    if count < 2:
        raise InvalidValue("a frame needs at least two samples", detail={"samples": samples})
    out: list[float] = []
    for index in range(count):
        t = index / count
        value = 0.0
        for number, amplitude, phase in rows:
            value += amplitude * math.sin(2.0 * math.pi * number * t + math.radians(phase))
        out.append(value)
    if normalize:
        peak = max((abs(value) for value in out), default=0.0)
        if peak > 0:
            out = [value / peak for value in out]
    return [_clamp(value) for value in out]


def _finite(value: float) -> bool:
    return not (math.isnan(value) or math.isinf(value))


def _clamp(value: float) -> float:
    return 1.0 if value > 1.0 else (-1.0 if value < -1.0 else value)


def _resample(values: Sequence[float], samples: int) -> list[float]:
    """Linear resampling to exactly ``samples`` points (endpoints included)."""
    out: list[float] = []
    span = len(values) - 1
    for index in range(samples):
        position = index * span / (samples - 1)
        low = int(math.floor(position))
        high = min(span, low + 1)
        frac = position - low
        out.append(float(values[low]) * (1.0 - frac) + float(values[high]) * frac)
    return out


def _frame_from_spec(spec: dict[str, Any], key: str, item: Any, index: int) -> list[float]:
    samples = FRAME_SAMPLES
    if key == "shape":
        return shape_wave(
            item, samples=samples, duty=spec.get("duty", 0.5), phase=spec.get("phase", 0.0)
        )
    if key == "harmonics":
        # normalised to peak 1.0: a spectrum's RATIOS are its timbre, and a frame that leaves
        # [-1, 1] would be a clipped frame. `harmonics_wave` keeps the raw sum available.
        return harmonics_wave(item, samples=samples, normalize=True)
    # raw samples: a list of numbers (one frame) or a list of lists (one per frame)
    if not isinstance(item, (list, tuple)) or not item:
        raise InvalidValue(
            "a `samples` frame must be a non-empty list of numbers",
            detail={"frame": index, "got": type(item).__name__},
        )
    values: list[float] = []
    for value in item:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise InvalidValue(
                "a `samples` frame may only carry numbers",
                detail={"frame": index, "value": value},
            ) from exc
        if not _finite(number):
            raise InvalidValue(
                "a sample may not be NaN or infinite",
                detail={"frame": index, "value": value},
            )
        values.append(_clamp(number))
    if len(values) == samples:
        return values
    if len(values) == 1:
        return [values[0]] * samples
    return _clamp_frame(_resample(values, samples))


def _clamp_frame(values: Sequence[float]) -> list[float]:
    return [_clamp(value) for value in values]


def _lerp_frames(left: Sequence[float], right: Sequence[float], frac: float) -> list[float]:
    return [
        left[index] * (1.0 - frac) + right[index] * frac for index in range(len(left))
    ]


def _items_for(key: str, raw: Any) -> list[Any]:
    """The ITEMS of a spec: one per key position on the morph axis.

    The three sources read a list differently, and the difference is what makes a morph writable
    without a second syntax:

    * ``shape`` - a name is one frame, a list of names is one frame per name
      (``["sine", "square"]`` is a two-item morph);
    * ``harmonics`` - a spectrum is a list of ``[partial, amplitude]`` rows (or a mapping), and a
      list of SUCH spectra is one spectrum per frame: the two are told apart by whether the first
      element is itself a row (numbers first) or a spectrum (a row/list first);
    * ``samples`` - a list of numbers is one frame, a list of lists is one frame per list.
    """
    if raw is None:
        return []
    if key == "shape":
        if isinstance(raw, (list, tuple)):
            return [item for item in raw]
        return [raw]
    if key == "harmonics":
        if isinstance(raw, dict):
            return [raw]
        if not isinstance(raw, (list, tuple)) or not raw:
            raise InvalidValue(
                "`harmonics` must be a list of [partial, amplitude] rows (or one such list per frame)",
                detail={"got": type(raw).__name__},
            )
        first = raw[0]
        if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple, dict)):
            return [item for item in raw]
        return [raw]
    # samples
    if not isinstance(raw, (list, tuple)) or not raw:
        raise InvalidValue(
            "`samples` must be a list of numbers (one frame) or a list of such lists",
            detail={"got": type(raw).__name__},
        )
    if isinstance(raw[0], (list, tuple)):
        return [item for item in raw]
    return [raw]


def spec_frames(spec: Any) -> list[list[float]]:
    """The frames a spec produces: validated, exactly ``FRAME_SAMPLES`` wide, clamped.

    ``frames`` semantics (the whole morph protocol): the listed items are the KEY positions, and
    ``frames`` (default: one per item) says how many frames the table holds. With more frames than
    items the frames between two items are interpolated, and the last item holds. Two shapes and
    ``frames: 16`` is therefore a 16-step morph from the first to the second.
    """
    if not isinstance(spec, dict):
        raise InvalidValue(
            "`table` must be an object describing the table",
            detail={"got": type(spec).__name__, "keys": list(SPEC_KEYS)},
        )
    unknown = [key for key in spec if key not in SPEC_KEYS]
    if unknown:
        raise InvalidValue(
            "the table spec carries keys this layer does not build",
            detail={"unknown": unknown, "accepted": list(SPEC_KEYS)},
        )
    sources = [key for key in SOURCE_KEYS if key in spec and spec[key] is not None]
    if len(sources) != 1:
        raise InvalidValue(
            "a table spec needs EXACTLY ONE source: `shape`, `harmonics` or `samples`",
            detail={"found": sources, "accepted": list(SOURCE_KEYS)},
        )
    key = sources[0]
    items = _items_for(key, spec[key])
    if not items:
        raise InvalidValue("the table spec carries no frames", detail={"source": key})
    wanted = spec.get("frames")
    if wanted is None:
        wanted = len(items)
    try:
        count = int(wanted)
    except (TypeError, ValueError) as exc:
        raise InvalidValue(
            "`frames` must be a whole number", detail={"frames": wanted}
        ) from exc
    if not 1 <= count <= MAX_FRAMES:
        raise InvalidValue(
            f"a table holds 1..{MAX_FRAMES} frames on this build",
            detail={"frames": wanted, "max": MAX_FRAMES},
        )
    keys = [
        _frame_from_spec(spec, key, item, index) for index, item in enumerate(items)
    ]
    if count == len(keys):
        return keys
    if count < len(keys):
        # narrowing: take the listed items evenly so the morph keeps its shape
        picked = [
            keys[min(len(keys) - 1, int(index * len(keys) / count))] for index in range(count)
        ]
        return picked
    out: list[list[float]] = []
    span = len(keys) - 1
    for index in range(count):
        position = index * span / (count - 1) if count > 1 else 0.0
        low = int(math.floor(position))
        high = min(span, low + 1)
        frac = position - low
        out.append(keys[low] if low == high else _lerp_frames(keys[low], keys[high], frac))
    return out


# ---------------------------------------------------------------------------
# The table object the plugin reads
# ---------------------------------------------------------------------------


def frame_positions(count: int) -> list[int]:
    """The axis position of each frame: measured corpus layout, ``round(i * 256 / N)``."""
    if count <= 0:
        return []
    if count == 1:
        return [0]
    return [int(round(index * 256 / count)) for index in range(count)]


def encode_frame(values: Sequence[float]) -> str:
    """One frame as the plugin stores it: base64 of little-endian float32, 2,048 samples."""
    if len(values) != FRAME_SAMPLES:
        raise InvalidValue(
            "a frame must be exactly the plugin's frame width",
            detail={"samples": len(values), "required": FRAME_SAMPLES},
        )
    return base64.b64encode(struct.pack("<%df" % len(values), *values)).decode("ascii")


def build_table(spec: Any, *, live_version: str | None = None) -> dict[str, Any]:
    """The plugin's own table object for ``spec``, ready for the state channel.

    The shape is the measured one (a `Wave Source` component with one keyframe per frame inside a
    single group), the frame positions follow the corpus layout, and `version` is stamped from the
    live document when one is given - the plugin restamps it on re-serialisation, so a value
    carrying anything else would make an exact read-back comparison report a landed write as a
    failure.
    """
    frames = spec_frames(spec)
    keyframes = [
        {"position": position, "wave_data": encode_frame(frame)}
        for position, frame in zip(frame_positions(len(frames)), frames)
    ]
    table: dict[str, Any] = {
        "author": str((spec or {}).get("author") or "reaper-reapy-mcp"),
        "full_normalize": bool((spec or {}).get("full_normalize") or False),
        "name": str((spec or {}).get("name") or _default_name(spec)),
        "remove_all_dc": bool((spec or {}).get("remove_all_dc") or False),
        "version": str(live_version or "1.5.5"),
        "groups": [
            {
                "components": [
                    {
                        "type": "Wave Source",
                        "interpolation": 1,
                        "interpolation_style": 1,
                        "keyframes": keyframes,
                    }
                ]
            }
        ],
    }
    return table


def _default_name(spec: Any) -> str:
    """A label for a built table: the plugin shows it, and a caller should recognise its own work.

    The name is NOT an identity anywhere in this layer (a digest is), so this is a convenience - but
    a nameless table in a browser list is worse than a boring one.
    """
    source = next((key for key in SOURCE_KEYS if isinstance(spec, dict) and spec.get(key) is not None), None)
    if source == "shape":
        value = spec.get("shape")
        label = "+".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
        return f"MCP {label}"[:60]
    if source == "harmonics":
        return "MCP harmonics"
    if source == "samples":
        return "MCP samples"
    return "MCP table"


# ---------------------------------------------------------------------------
# Reading back: what a frame IS, in numbers a caller can reason about
# ---------------------------------------------------------------------------


def frame_summary(values: Sequence[float], *, points: int = 8, harmonics: int = 6) -> dict[str, Any]:
    """A compact description of one frame: a few samples, peak/RMS, and its own spectrum.

    The spectrum is what makes a read-back checkable by a caller who cannot see the waveform: the
    magnitude of the first ``harmonics`` partials of the frame as stored, so "did my saw arrive" is
    a number and not an opinion.
    """
    data = [float(value) for value in values]
    if not data:
        return {"samples": 0}
    peak = max(abs(value) for value in data)
    rms = math.sqrt(sum(value * value for value in data) / len(data))
    step = max(1, len(data) // max(1, points))
    sampled = [round(data[index], 4) for index in range(0, len(data), step)][:points]
    spectrum: list[float] = []
    count = len(data)
    for harmonic in range(1, max(1, harmonics) + 1):
        real = 0.0
        imag = 0.0
        for index, value in enumerate(data):
            angle = 2.0 * math.pi * harmonic * index / count
            real += value * math.cos(angle)
            imag += value * math.sin(angle)
        spectrum.append(round(2.0 * math.hypot(real, imag) / count, 4))
    return {
        "samples": count,
        "peak": round(peak, 4),
        "rms": round(rms, 4),
        "first_samples": sampled,
        "partials": spectrum,
        "dc": round(sum(data) / count, 4),
    }


def describe_spec(spec: Any) -> dict[str, Any]:
    """What the spec IS, without building the payloads: counts, cost, and the source it names."""
    frames = spec_frames(spec)
    chars = sum(len(encode_frame(frame)) for frame in frames)
    return {
        "frames": len(frames),
        "samples_per_frame": FRAME_SAMPLES,
        "source": next(
            (key for key in SOURCE_KEYS if isinstance(spec, dict) and spec.get(key) is not None),
            None,
        ),
        "positions": frame_positions(len(frames)),
        "payload_chars": chars,
        "payload_bytes": chars * 3 // 4,
        "name": _default_name(spec) if isinstance(spec, dict) else None,
    }


def _picked_indices(count: int, wanted: int) -> list[int]:
    """Which frames to describe: the ends of the morph first, then evenly spaced ones.

    A caller designing a morph needs the two ends to know what it built, so ``wanted=2`` on an
    16-frame table must answer frames 0 and 15 - not 0 and 1.
    """
    if count <= 0:
        return []
    if wanted >= count:
        return list(range(count))
    if wanted <= 1 or count == 1:
        return [0]
    return sorted({int(round(index * (count - 1) / (wanted - 1))) for index in range(wanted)})


def preview(spec: Any, *, frames: int = 2, points: int = 8) -> dict[str, Any]:
    """A few frames of the spec, summarised - the host-free way to see what will be written.

    ``frames`` (default 2 for a morph, 1 for a single-frame spec) selects which frames are described:
    the first and the last for a morph, because those are its two ends.
    """
    built = spec_frames(spec)
    picked = _picked_indices(len(built), max(1, int(frames)))
    return {
        "described": describe_spec(spec),
        "frames": [
            {"frame": index, **frame_summary(built[index], points=points)} for index in picked
        ],
        "frame_indices": picked,
        "shapes": list(SHAPES),
    }


# ---------------------------------------------------------------------------
# The capability `plugin_wavetable` uses
# ---------------------------------------------------------------------------


class WavetableCapability:
    """Build, preview and read Vital's wavetables - the vocabulary `plugin_wavetable` speaks.

    A provider without this hook is told so by name (the tool layer refuses typed), and everything
    plugin-specific about a table stays on this side of the boundary: the frame width, the axis, the
    component shape, and the fact that the plugin restamps its own version.
    """

    #: The tool reports this so a caller can discover the vocabulary without guessing.
    shapes = SHAPES
    max_frames = MAX_FRAMES
    frame_samples = FRAME_SAMPLES

    def slot_path(self, oscillator: Any) -> str:
        """The state path of an oscillator's table slot (`settings.wavetables.<n>`)."""
        from . import wavetables as wavetables_module

        return wavetables_module.path_for(oscillator)

    def build(self, spec: Any, *, live_version: str | None) -> dict[str, Any]:
        """The table object for a spec, with its own description attached for the wire note."""
        table = build_table(spec, live_version=live_version)
        return {"table": table, "described": describe_spec(spec)}

    def preview(self, spec: Any, *, frames: int = 2, points: int = 8) -> dict[str, Any]:
        return preview(spec, frames=frames, points=points)

    def read(self, document: Any, *, oscillator: Any, frames: int = 2, points: int = 8) -> dict[str, Any]:
        """What the addressed slot HOLDS right now: identity, size, and the frames' own numbers.

        Only descriptors and a few frames' summaries: the wave data of a big table is megabytes, and
        a caller who wants the bytes wants a different tool (the state channel).
        """
        from . import wavetables as wavetables_module

        slot = wavetables_module.check_slot(oscillator)
        tables = (
            ((document or {}).get("settings") or {}).get(wavetables_module.SETTINGS_KEY)
            if isinstance(document, dict)
            else None
        )
        if not isinstance(tables, list) or len(tables) < slot:
            raise InvalidValue(
                f"the live document has no wavetable for oscillator {slot}",
                detail={"oscillator": slot, "slots_present": len(tables) if isinstance(tables, list) else 0},
            )
        table = tables[slot - 1]
        described = wavetables_module.describe(table, oscillator=slot)
        summaries: list[dict[str, Any]] = []
        rows = wavetables_module.keyframes(table)
        picked = _picked_indices(len(rows), max(1, int(frames)))
        for index in picked:
            decoded = wavetables_module.decode_frame(table, index)
            if decoded:
                summaries.append({"frame": index, **frame_summary(decoded, points=points)})
        described["frame_summaries"] = summaries
        described["frame_indices"] = picked
        described["shapes"] = list(SHAPES)
        return described

