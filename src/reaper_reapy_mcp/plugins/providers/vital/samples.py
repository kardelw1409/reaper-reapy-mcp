"""Vital's SAMPLER as a channel: load a waveform into the sample engine (P41).

What the sampler IS, from the plugin's own source and the live measurements:

* the whole sampler payload lives in ONE state block, ``settings.sample``: ``name``,
  ``length``, ``sample_rate`` and the PCM as base64 int16 - ``samples`` is the LEFT channel
  and ``samples_stereo`` the RIGHT one when the buffer is stereo
  (``Sample::stateToJson`` encodes ``left_buffers`` into ``samples`` and ``right_buffers``
  into ``samples_stereo``; ``jsonToState`` decodes exactly ``length * sizeof(int16_t)``
  bytes, so a payload must carry exactly ``length`` samples - longer is silently cut, a
  shorter one is zero-padded by the plugin);
* the block is an ORDINARY state path: the measured landing bound, the sample balloon, the
  read-back verification and the rollback all apply (measured live 2026-10-18: a written
  block reads back with the same ``name``/``length``/``sample_rate`` and its payload delayed
  by the measured 4 samples). This is how presets carry their sample
  (``sample_inflated_for_write``);
* the leaf ``settings.sample.samples`` may NOT be addressed on its own: the plugin
  re-encodes it on every state load, so a write of the leaf cannot be verified (codec's
  ``DERIVED_LEAVES``). The BLOCK is the addressable unit and that is what this module builds;
* ``Sample::loadSample`` clamps a mono buffer to ``kMaxSize`` = 1,764,000 samples (the
  stereo overload does not clamp, so the refusal here is the layer's own honest bound);
* the sampler's own controls are ordinary parameters and ride the same batch
  (``sample_on`` / ``sample_loop`` / ``sample_destination`` / ``sample_level`` / ...).

Reading a WAV file: the stdlib `wave` reader is tried first (it is the format's reference
implementation for integer PCM), and anything it refuses is handed to this module's own RIFF
reader, which also covers the shapes sample packs actually ship - measured on the owner's own
packs: `fmt=3 ch=2 rate=44100 bits=32`, i.e. 32-bit FLOAT stereo, which `wave` reports as
"unknown format: 3" and refuses. The layer therefore reads integer PCM at 8/16/24/32 bits, IEEE
float at 32/64, and either of those wrapped in WAVE_FORMAT_EXTENSIBLE.
"""

from __future__ import annotations

import base64
import sys
import wave
from pathlib import Path
from typing import Any

from ...errors import InvalidValue, Unreadable
from . import codec as codec_module

__all__ = [
    "PCM_MAX",
    "PCM_SCALE",
    "SAMPLER_CONTROLS",
    "SOURCE_CONTROLS",
    "SampleCapability",
    "build_block",
    "describe",
    "read_riff_wav",
    "read_wav",
]

#: Caller-facing knob -> the provider's own control id. The vocabulary is the PROFILE's
#: (measured: the `Sample` module holds eleven controls), and this mapping is the only
#: place that knows both spellings.
SAMPLER_CONTROLS: dict[str, str] = {
    "on": "sample_on",
    "level": "sample_level",
    "loop": "sample_loop",
    "destination": "sample_destination",
    "transpose": "sample_transpose",
    "tune": "sample_tune",
    "pan": "sample_pan",
    "keytrack": "sample_keytrack",
    "bounce": "sample_bounce",
    "random_phase": "sample_random_phase",
}

#: The plugin's OTHER sound sources, as the switches that turn them off.
#:
#: The sampler is a fourth source beside the three wavetable oscillators (measured: the profile
#: declares `osc_<n>_on` for each of them plus `sample_on`), so "make the sampler the thing you
#: hear" means switching three controls off. A caller should not have to know that, and the core
#: must not hard-code it - this table is the provider's, and `solo` reads it.
SOURCE_CONTROLS: tuple[str, ...] = ("osc_1_on", "osc_2_on", "osc_3_on")

#: The sampler's buffer bound, from the plugin's own source (`Sample::loadSample`).
MAX_SAMPLES = codec_module.SAMPLE_MAX_SAMPLES


def _pcm_from_int(raw: bytes, width: int) -> list[int]:
    """One channel's bytes as int16 values, for the integer PCM widths a WAV can carry.

    8-bit WAV data is UNSIGNED (the format's own convention); 16-bit is int16; 24-bit is
    sign-extended from three bytes and its low 8 bits are dropped (the plugin plays int16, so the
    top 16 bits ARE the sample); 32-bit keeps the top 16. A width outside this set is refused
    rather than guessed - a wrong shift is a silent amplitude error.
    """
    import array

    if width == 1:
        data = array.array("B")
        data.frombytes(raw)
        return [(value - 128) * 256 for value in data]
    if width == 2:
        data = array.array("h")
        data.frombytes(raw)
        return list(data)
    if width == 3:
        out: list[int] = []
        for index in range(0, len(raw) - 2, 3):
            value = raw[index] | (raw[index + 1] << 8) | (raw[index + 2] << 16)
            if value & 0x800000:
                value -= 1 << 24
            out.append(value >> 8)
        return out
    if width == 4:
        if sys.byteorder != "little":  # pragma: no cover - the hosts this runs on are little-endian
            raise Unreadable(
                "32-bit PCM on a big-endian host is not converted by this layer",
                detail={"byteorder": sys.byteorder},
            )
        data = array.array("i")
        data.frombytes(raw)
        return [value >> 16 for value in data]
    raise InvalidValue(
        f"a {width * 8}-bit integer PCM sample width is not converted by this layer",
        detail={"bits": width * 8, "accepted": [8, 16, 24, 32]},
    )


def _pcm_from_float(raw: bytes, width: int) -> list[int]:
    """IEEE-float WAV data as int16, at the plugin's OWN scale.

    The plugin's own conversion is `utils::floatToPcmData`: ``clamp(value * kPcmScale, -kPcmScale,
    kPcmScale)`` with ``kPcmScale = 32767`` (`utils.cpp`). This mirrors that scale and rounds to
    the nearest step instead of truncating toward zero - at most one LSB away from the plugin's own
    arithmetic, and inside the channel's measured requant band either way.
    """
    import array

    kind = "f" if width == 4 else "d"
    data = array.array(kind)
    data.frombytes(raw[: len(raw) // (width) * width])
    if sys.byteorder == "big":  # pragma: no cover
        data.byteswap()
    out: list[int] = []
    for value in data:
        scaled = float(value) * PCM_SCALE
        if scaled >= PCM_SCALE:
            out.append(PCM_MAX)
        elif scaled <= -PCM_SCALE:
            out.append(-PCM_MAX)
        else:
            out.append(int(scaled + (0.5 if scaled >= 0 else -0.5)))
    return out


def _pcm_from_bytes(raw: bytes, width: int, channels: int, *, is_float: bool) -> tuple[list[int], ...]:
    """De-interleave ``raw`` into int16 channels."""
    samples = _pcm_from_float(raw, width) if is_float else _pcm_from_int(raw, width)
    if channels <= 1:
        return (samples,)
    # more than two channels: the plugin's engine is mono-or-stereo, so taking the first two is
    # the only faithful reading - and the caller is told how many were dropped
    return (samples[0::channels], samples[1::channels])


def _chunks(raw: bytes) -> dict[str, bytes]:
    """The RIFF chunks of a WAVE file, keyed by id (`fmt `, `data`, ...).

    Minimal on purpose: a WAV is a RIFF container of length-prefixed, word-aligned chunks, and the
    two this layer reads are the format and the samples. Every other chunk (LIST, fact, cue, smpl,
    bext) is skipped by its own length, and a chunk whose declared size runs past the file is
    CLIPPED to what is there - a truncated file must not raise an IndexError.
    """
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise InvalidValue(
            "the file is not a RIFF/WAVE file",
            detail={"reason": "not_riff_wave", "head": raw[:4].decode("latin-1", "replace")},
        )
    out: dict[str, bytes] = {}
    offset = 12
    while offset + 8 <= len(raw):
        chunk_id = raw[offset:offset + 4].decode("latin-1", "replace")
        size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        start = offset + 8
        end = min(start + size, len(raw))
        if chunk_id and chunk_id not in out:
            out[chunk_id] = raw[start:end]
        offset = start + size + (size & 1)
    return out


#: The format codes a WAV can carry that this layer understands. `0xFFFE` is
#: WAVE_FORMAT_EXTENSIBLE: the real code sits in the first two bytes of its SubFormat GUID, and
#: most modern DAW exports use it for anything above 16-bit.
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

#: The plugin's own scale (`utils.cpp`: `kPcmScale = 32767.0f`). Its conversion clamps to
#: +/-32767, so -32768 is never produced by the plugin's own arithmetic.
PCM_SCALE = 32767.0
PCM_MAX = 32767


def read_riff_wav(path: Path) -> dict[str, Any]:
    """Read a WAV the stdlib refuses: float, extensible, or 24-bit (P41).

    Python's stdlib `wave` handles integer PCM only and reports everything else as "unknown
    format" - which is exactly the shape most sample packs ship (measured on the owner's own packs:
    `fmt=3 ch=2 rate=44100 bits=32`, i.e. 32-bit float stereo, refused by `wave`). This reader
    accepts what the format defines and the plugin can play: integer PCM at 8/16/24/32 bits, IEEE
    float at 32/64, and either of those wrapped in WAVE_FORMAT_EXTENSIBLE.
    """
    raw = path.read_bytes()
    chunks = _chunks(raw)
    fmt = chunks.get("fmt ")
    data = chunks.get("data")
    if fmt is None or len(fmt) < 16:
        raise InvalidValue(
            f"{path.name!r} carries no readable `fmt ` chunk",
            detail={"path": str(path), "reason": "no_fmt_chunk", "chunks": sorted(chunks)},
        )
    if data is None:
        raise InvalidValue(
            f"{path.name!r} carries no `data` chunk",
            detail={"path": str(path), "reason": "no_data_chunk", "chunks": sorted(chunks)},
        )
    code = int.from_bytes(fmt[0:2], "little")
    channels = int.from_bytes(fmt[2:4], "little")
    rate = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if code == WAVE_FORMAT_EXTENSIBLE:
        if len(fmt) < 26:
            raise InvalidValue(
                f"{path.name!r} declares WAVE_FORMAT_EXTENSIBLE without a SubFormat",
                detail={"path": str(path), "reason": "extensible_without_subformat", "fmt_bytes": len(fmt)},
            )
        code = int.from_bytes(fmt[24:26], "little")
    if code not in (WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT):
        raise InvalidValue(
            f"{path.name!r} carries format code {code}, which is not PCM or IEEE float",
            detail={
                "path": str(path),
                "format_code": code,
                "accepted": {"1": "PCM", "3": "IEEE float", "65534": "extensible (their SubFormat)"},
                "why": "a compressed WAV (ADPCM, mu-law, ...) would have to be decoded by something "
                       "this layer does not have; convert the file to PCM or float WAV",
            },
        )
    is_float = code == WAVE_FORMAT_FLOAT
    if is_float and bits not in (32, 64):
        raise InvalidValue(
            f"{path.name!r} declares IEEE float at {bits} bits",
            detail={"path": str(path), "bits": bits, "accepted": [32, 64]},
        )
    if not is_float and bits not in (8, 16, 24, 32):
        raise InvalidValue(
            f"{path.name!r} declares {bits}-bit integer PCM",
            detail={"path": str(path), "bits": bits, "accepted": [8, 16, 24, 32]},
        )
    if channels < 1:
        raise InvalidValue(
            f"{path.name!r} declares no channels",
            detail={"path": str(path), "channels": channels},
        )
    frame_bytes = max(1, (bits // 8) * channels)
    frames = len(data) // frame_bytes
    decoded = _pcm_from_bytes(data[: frames * frame_bytes], bits // 8, channels, is_float=is_float)
    left = decoded[0]
    right = decoded[1] if len(decoded) > 1 else None
    return {
        "path": str(path),
        "name": path.stem,
        "sample_rate": rate,
        "bits": bits,
        "file_channels": channels,
        "frames": len(left),
        "left": left,
        "right": right,
        "dropped_channels": max(0, channels - 2),
        "encoding": "float" if is_float else "pcm",
        "reader": "riff",
    }


def read_wav(path: str) -> dict[str, Any]:
    """One WAV file as decoded int16 channels, or a typed refusal.

    The stdlib `wave` reader is tried FIRST (it is the format's reference implementation for
    integer PCM); anything it refuses is handed to the layer's own RIFF reader, which also covers
    the float and extensible shapes sample packs ship. Which reader answered is reported
    (`reader`), so a caller never has to guess what read the file.
    """
    candidate = Path(str(path))
    if not candidate.is_file():
        raise InvalidValue(
            f"no file at {path!r}",
            detail={"path": str(path), "reason": "not_found"},
        )
    try:
        handle = wave.open(str(candidate), "rb")
    except wave.Error:
        # the stdlib knows integer PCM only: float, extensible and 24-bit land here
        return _bounded(path=candidate, decoded=read_riff_wav(candidate))
    except OSError as exc:
        raise Unreadable(
            f"the file could not be opened: {exc}", detail={"path": str(candidate)}
        ) from exc
    with handle:
        channels = int(handle.getnchannels())
        width = int(handle.getsampwidth())
        rate = int(handle.getframerate())
        frames = int(handle.getnframes())
        comptype = str(handle.getcomptype())
        if comptype != "NONE":
            raise InvalidValue(
                f"the WAV is compressed ({comptype}), which this layer does not decode",
                detail={"path": str(candidate), "compression": comptype, "accepted": "NONE (PCM)"},
            )
        raw = handle.readframes(frames)
    decoded = _pcm_from_bytes(raw, width, channels, is_float=False)
    left = decoded[0]
    right = decoded[1] if len(decoded) > 1 else None
    return _bounded(
        path=candidate,
        decoded={
            "path": str(candidate),
            "name": candidate.stem,
            "sample_rate": rate,
            "bits": width * 8,
            "file_channels": channels,
            "frames": min(len(left), frames),
            "left": left,
            "right": right,
            "dropped_channels": max(0, channels - 2),
            "encoding": "pcm",
            "reader": "wave",
        },
    )


def _bounded(*, path: Path, decoded: dict[str, Any]) -> dict[str, Any]:
    """Refuse a sample longer than the plugin's own buffer bound (never truncate it)."""
    frames = int(decoded["frames"])
    if frames > MAX_SAMPLES:
        raise InvalidValue(
            "the sample is longer than the plugin's own buffer bound",
            detail={
                "path": str(path),
                "frames": frames,
                "max_samples": MAX_SAMPLES,
                "why": "`Sample::loadSample` clamps its buffer to kMaxSize = 1,764,000 samples "
                       "(measured in the plugin's source), so a longer file cannot be delivered "
                       "through this channel - trim the file first",
            },
        )
    return decoded


def build_block(
    *,
    left: list[int],
    right: list[int] | None,
    sample_rate: int,
    name: str,
) -> dict[str, Any]:
    """The state block for one sample buffer (exactly `length` samples per channel)."""
    if not left:
        raise InvalidValue(
            "the sample is empty, so there is nothing to load",
            detail={"samples": len(left)},
        )
    if len(left) > MAX_SAMPLES:
        raise InvalidValue(
            "the sample is longer than the plugin's own buffer bound",
            detail={"samples": len(left), "max_samples": MAX_SAMPLES},
        )
    if right is not None and len(right) != len(left):
        raise InvalidValue(
            "the two channels must be the same length",
            detail={"left": len(left), "right": len(right)},
        )
    block: dict[str, Any] = {
        "name": str(name),
        "length": int(len(left)),
        "sample_rate": int(sample_rate),
        "samples": base64.b64encode(_as_int16(left)).decode("ascii"),
    }
    if right is not None:
        block["samples_stereo"] = base64.b64encode(_as_int16(right)).decode("ascii")
    return block


def _as_int16(values: list[Any]) -> bytes:
    """int16 little-endian bytes; a value outside the range is CLAMPED, never wrapped.

    Wrapping would turn a too-loud transient into the opposite polarity, which is a silent
    corruption of the material; clamping is the ordinary sampler behaviour and is visible.
    """
    import array

    data = array.array("h", (max(-32768, min(32767, int(round(float(value))))) for value in values))
    if sys.byteorder == "big":  # pragma: no cover - the hosts this runs on are little-endian
        data.byteswap()
    return data.tobytes()


def describe(document: Any) -> dict[str, Any]:
    """What the sampler HOLDS now: identity, size, channels, seconds - not the PCM."""
    block = codec_module.dig(document, codec_module.SAMPLE_PATH)
    if not isinstance(block, dict):
        raise InvalidValue(
            "the live document carries no sample block",
            detail={"path": codec_module.SAMPLE_PATH},
        )
    length = block.get("length")
    rate = block.get("sample_rate")
    payload_chars = {
        key: len(block[key]) for key in codec_module.SAMPLE_PAYLOAD_KEYS if isinstance(block.get(key), str)
    }
    described: dict[str, Any] = {
        "path": codec_module.SAMPLE_PATH,
        "name": block.get("name"),
        "length": length,
        "sample_rate": rate,
        "channels": 2 if isinstance(block.get("samples_stereo"), str) else 1,
        "payload_chars": payload_chars,
        "payload_bytes": sum(payload_chars.values()) * 3 // 4,
    }
    if isinstance(length, int) and isinstance(rate, int) and rate > 0:
        described["seconds"] = round(length / rate, 6)
    described["max_samples"] = MAX_SAMPLES
    return described


class SampleCapability:
    """Load and read Vital's sampler - the vocabulary `plugin_sample` speaks.

    Everything plugin-specific (the block's key names, the channel mapping, the buffer bound)
    stays on this side of the boundary; the tool layer only sees `load` / `read`.
    """

    controls = SAMPLER_CONTROLS
    sources = SOURCE_CONTROLS
    max_samples = MAX_SAMPLES

    def solo_values(self) -> dict[str, str]:
        """The control write that leaves ONLY the sampler sounding.

        Returned as the control ids and their own enum labels ("On"/"Off"), which is what the
        declaration takes. The label is spelled from the plugin's own vocabulary rather than a
        member index, so the write cannot land in the wrong band.
        """
        values = {control: "Off" for control in self.sources}
        values[str(self.controls["on"])] = "On"
        return values

    def slot_path(self) -> str:
        """The state path of the sampler block (`settings.sample`)."""
        return codec_module.SAMPLE_PATH

    def load(
        self,
        *,
        file_path: str | None = None,
        samples: Any = None,
        sample_rate: Any = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Build the block for a file OR a raw list of int16 samples.

        Exactly one source is required, and it is checked here rather than in the tool layer so
        the rule holds for any caller: a request naming both would have to pick one silently.
        """
        if file_path and samples is not None:
            raise InvalidValue(
                "name ONE source: either `file_path` (a WAV) or `samples` (raw int16 values)",
                detail={"file_path": file_path, "samples_count": len(samples or [])},
            )
        if file_path:
            decoded = read_wav(str(file_path))
            block = build_block(
                left=[int(value) for value in decoded["left"]],
                right=[int(value) for value in decoded["right"]] if decoded["right"] is not None else None,
                sample_rate=int(decoded["sample_rate"]),
                name=str(name or decoded["name"]),
            )
            described = {
                "source": "file",
                "file": decoded["path"],
                "file_channels": decoded["file_channels"],
                "file_bits": decoded["bits"],
                "file_encoding": decoded.get("encoding"),
                "reader": decoded.get("reader"),
                "dropped_channels": decoded["dropped_channels"],
                "sample_rate": decoded["sample_rate"],
            }
        elif samples is not None:
            if not isinstance(samples, (list, tuple)) or not samples:
                raise InvalidValue(
                    "`samples` must be a non-empty list of int16 values",
                    detail={"type": type(samples).__name__},
                )
            rate = int(sample_rate or 0)
            if rate <= 0:
                raise InvalidValue(
                    "raw `samples` need `sample_rate` (the plugin plays a buffer at that rate)",
                    detail={"sample_rate": sample_rate},
                )
            mono: list[int] = []
            stereo: list[int] | None = None
            if samples and isinstance(samples[0], (list, tuple)):
                # a list of channels: [left, right]
                if len(samples) != 2:
                    raise InvalidValue(
                        "a channel list must hold exactly two rows (left, right)",
                        detail={"rows": len(samples)},
                    )
                left = [int(value) for value in samples[0]]
                right = [int(value) for value in samples[1]]
                block = build_block(left=left, right=right, sample_rate=rate, name=str(name or "MCP Sample"))
            else:
                mono = [int(value) for value in samples]
                block = build_block(left=mono, right=stereo, sample_rate=rate, name=str(name or "MCP Sample"))
            described = {"source": "samples", "sample_rate": rate}
        else:
            raise InvalidValue(
                "provide `file_path` (a WAV) or `samples` (raw int16 values) - one of them",
                detail={"file_path": file_path, "samples": samples},
            )
        described.update(
            {
                "name": block["name"],
                "length": block["length"],
                "channels": 2 if "samples_stereo" in block else 1,
                "payload_chars": sum(
                    len(block[key]) for key in codec_module.SAMPLE_PAYLOAD_KEYS if key in block
                ),
            }
        )
        return {"block": block, "described": described}

    def read(self, document: Any) -> dict[str, Any]:
        return describe(document)
