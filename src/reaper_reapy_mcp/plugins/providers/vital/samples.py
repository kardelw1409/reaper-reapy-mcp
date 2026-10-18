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

Reading a WAV file is deliberately STDLIB ONLY (``wave``): a sampler loader that needed a
third-party decoder would not be usable from a bare environment, and the formats it accepts
are exactly the ones the stdlib guarantees (PCM 8/16-bit, and 32-bit PCM as int).
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
    "SAMPLER_CONTROLS",
    "SampleCapability",
    "build_block",
    "describe",
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

#: The sampler's buffer bound, from the plugin's own source (`Sample::loadSample`).
MAX_SAMPLES = codec_module.SAMPLE_MAX_SAMPLES


def _pcm_from_bytes(raw: bytes, width: int, channels: int) -> tuple[list[float], ...]:
    """De-interleave ``raw`` into int16 channels (scaled by 32767/2**15 semantics).

    Only the widths the stdlib can hand over are accepted, and each conversion is named:
    8-bit WAV data is UNSIGNED (the format's own convention), 16-bit is int16,
    32-bit is int32 scaled down. 24-bit is refused rather than guessed: a wrong shift is a
    silent amplitude error, and the caller can convert the file once and be sure.
    """
    import array

    if width == 1:
        data = array.array("B")
        data.frombytes(raw)
        samples = [(value - 128) * 256 for value in data]
    elif width == 2:
        data = array.array("h")
        data.frombytes(raw)
        samples = list(data)
    elif width == 4:
        if sys.byteorder != "little":  # pragma: no cover - Windows/x86 hosts are little-endian
            raise Unreadable(
                "32-bit PCM on a big-endian host is not converted by this layer",
                detail={"byteorder": sys.byteorder},
            )
        data = array.array("i")
        data.frombytes(raw)
        samples = [value >> 16 for value in data]
    else:
        raise InvalidValue(
            f"a {width * 8}-bit PCM sample width is not converted by this layer",
            detail={
                "bits": width * 8,
                "accepted": [8, 16, 32],
                "why": "a 24-bit payload needs a shift this layer will not guess; convert the "
                       "file to 16-bit PCM first",
            },
        )
    if channels <= 1:
        return (samples,)
    left = samples[0::channels]
    right = samples[1::channels]
    extra = channels - 2
    if extra:
        # more than two channels: the plugin's engine is mono-or-stereo, so taking the first
        # two is the only faithful reading - and the caller is told how many were dropped
        return (left, right)
    return (left, right)


def read_wav(path: str) -> dict[str, Any]:
    """One PCM WAV file as interleaved-decoded int16 channels, or a typed refusal."""
    candidate = Path(str(path))
    if not candidate.is_file():
        raise InvalidValue(
            f"no file at {path!r}",
            detail={"path": str(path), "reason": "not_found"},
        )
    try:
        handle = wave.open(str(candidate), "rb")
    except wave.Error as exc:
        raise InvalidValue(
            f"{candidate.name!r} is not a WAV file this layer can read: {exc}",
            detail={
                "path": str(candidate),
                "reason": "not_pcm_wav",
                "why": "the stdlib `wave` reader accepts uncompressed PCM (and reports anything "
                       "else here) - convert the file to 16-bit PCM WAV if it is compressed or "
                       "float-encoded",
            },
        ) from exc
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
        if frames > MAX_SAMPLES:
            raise InvalidValue(
                "the sample is longer than the plugin's own buffer bound",
                detail={
                    "path": str(candidate),
                    "frames": frames,
                    "max_samples": MAX_SAMPLES,
                    "why": "`Sample::loadSample` clamps its buffer to kMaxSize = 1,764,000 samples "
                           "(measured in the plugin's source), so a longer file cannot be delivered "
                           "through this channel - trim the file first",
                },
            )
        raw = handle.readframes(frames)
    decoded = _pcm_from_bytes(raw, width, channels)
    left = decoded[0]
    right = decoded[1] if len(decoded) > 1 else None
    return {
        "path": str(candidate),
        "name": candidate.stem,
        "sample_rate": rate,
        "bits": width * 8,
        "file_channels": channels,
        "frames": min(len(left), frames),
        "left": left,
        "right": right,
        "dropped_channels": max(0, channels - 2),
    }


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
    described["note"] = (
        "the sampler is an ORDINARY state block: it carries the PCM inline, and its controls "
        "(`on`, `loop`, `destination`, `level`, ...) are ordinary parameters"
    )
    return described


class SampleCapability:
    """Load and read Vital's sampler - the vocabulary `plugin_sample` speaks.

    Everything plugin-specific (the block's key names, the channel mapping, the buffer bound)
    stays on this side of the boundary; the tool layer only sees `load` / `read`.
    """

    controls = SAMPLER_CONTROLS
    max_samples = MAX_SAMPLES

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
