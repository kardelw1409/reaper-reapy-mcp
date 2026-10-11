"""P19 shared live-probe helpers: call a tool, render a window, measure, restore.

Deliberately small and explicit - the same shape the P18 acceptance probe uses,
factored out so each diagnostic probes one question and the evidence stays
comparable. Nothing here writes by itself; the probes own their mutations and
their restores.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

import live_harness as H

TRACK_NAME = "V3-A Deep Glide 808"
ITEM_LENGTH = 4.0
PHRASE = [
    {"pitch": 28, "start": 0.2, "length": 0.9, "velocity": 96},  # E1 41.20 Hz
    {"pitch": 31, "start": 0.9, "length": 0.9, "velocity": 96},  # G1 49.00 Hz, overlaps n1
    {"pitch": 33, "start": 1.6, "length": 0.8, "velocity": 96},  # A1 55.00 Hz, overlaps n2
    {"pitch": 28, "start": 2.6, "length": 1.0, "velocity": 96},  # back down (forced glide)
]
NOTES_HZ = {28: 41.2034446, 31: 48.9994295, 33: 54.9999999}
FIRST_NOTE = (0.20, 1.10)  # the phrase's first note window (E1)
STEADY = {"n1": (0.30, 0.80), "n2": (1.30, 1.55), "n3": (1.90, 2.30)}
GLIDES = {"up_1": (0.92, 1.10), "up_2": (1.62, 1.80), "down": (2.62, 2.90)}
ATTACK_WINDOW = (0.18, 0.55)

#: How long the pitch drop's flight lasts (the P19 808 recipe's `env_2` decay is
#: 60 ms): the window a short-frame instrument must cover, since a three-period
#: autocorrelation window (73 ms at 41 Hz) cannot resolve it.
DROP_FLIGHT = 0.05

#: The smallest normalized autocorrelation peak still called a measurement. A
#: shorter window lets the bias-corrected estimator's tail noise grow, so a weak
#: peak is reported as *no measurement* (`None`) rather than as a pitch: an
#: absence of evidence must not be dressed as a reading (the P19 trap where a
#: biased peak at the edge of the search range read as +39.6 semitones).
MIN_AUTOCORR_PEAK = 0.4

#: How close to the tallest corrected peak a local maximum must be to be taken as
#: the fundamental. Bias correction equalizes the peaks at 1x, 2x and 3x the
#: period, so the *search rule* has to select the shortest one (see
#: `autocorr_f0`); without it a 55 Hz sub was read as 27.5 Hz.
NEAR_PEAK = 0.9

RENDER_INT_KEYS = (
    "RENDER_BOUNDSFLAG",
    "RENDER_SETTINGS",
    "RENDER_CHANNELS",
    "RENDER_SRATE",
    "RENDER_TAILFLAG",
    "RENDER_TAILMS",
)
RENDER_STR_KEYS = ("RENDER_FILE", "RENDER_PATTERN")


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


def must(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    payload = call(mcp, name, arguments)
    if payload.get("status") not in (None, "success") or payload.get("ok") is False:
        raise AssertionError(f"{name} failed: {json.dumps(payload, default=str)[:800]}")
    return payload


def track_by_name(mcp: Any, name: str = TRACK_NAME) -> int:
    listing = call(mcp, "get_track_list", {})
    rows = [row for row in listing.get("tracks") or [] if row.get("name") == name]
    if len(rows) != 1:
        raise AssertionError(f"expected exactly one track named {name!r}: {rows}")
    return int(rows[0]["index"])


def paragraph_window(mcp: Any, track_index: int) -> tuple[float, Any]:
    """The phrase item's window start and item id (reused, never re-created)."""
    items = must(mcp, "get_items_in_time_range", {"track_index": track_index, "start_time": 0.0, "end_time": 1200.0})
    phrase = [item for item in (items.get("items") or []) if abs(float(item["length"]) - ITEM_LENGTH) < 0.01]
    if not phrase:
        raise AssertionError("the phrase item is missing; this probe refuses to create one silently")
    item = phrase[0]
    return float(item["position"]), item.get("direct_item_id") or item.get("track_pos_idx")


def host_display(track: Any, index: int) -> str:
    """The plugin's OWN formatted value for a parameter index (the independent channel)."""
    from reapy import reascript_api as RPR

    return str(RPR.TrackFX_GetFormattedParamValue(track, 0, index, "", 512)[4])


def host_norm(track: Any, index: int) -> float:
    from reapy import reascript_api as RPR

    return float(RPR.TrackFX_GetParamNormalized(track, 0, index))


def render_window(mcp: Any, project: Any, output: Path, start: float, length: float = ITEM_LENGTH) -> dict:
    """Render `[start, start+length)` with the project's render settings restored."""
    from reapy import reascript_api as RPR

    pid = project.id
    int_before = {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in RENDER_INT_KEYS}
    str_before = {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in RENDER_STR_KEYS}
    selection_before = (project.time_selection.start, project.time_selection.end)
    try:
        payload = call(
            mcp,
            "render_project",
            {
                "output_file": str(output),
                "start_time": start,
                "end_time": start + length,
                "samplerate": 44100,
                "channels": 2,
                "kwargs": {},
            },
        )
    finally:
        for key, value in int_before.items():
            RPR.GetSetProjectInfo(pid, key, value, True)
        for key, value in str_before.items():
            RPR.GetSetProjectInfo_String(pid, key, value, True)
    ok = (
        {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in RENDER_INT_KEYS} == int_before
        and {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in RENDER_STR_KEYS} == str_before
        and (project.time_selection.start, project.time_selection.end) == selection_before
    )
    if payload.get("status") != "success" or not output.is_file():
        raise AssertionError(f"render failed: {payload}")
    return {"file": str(output), "settings_restored": ok}


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def autocorr_f0(segment: np.ndarray, fs: int, fmin: float = 25.0, fmax: float = 400.0) -> float | None:
    """The fundamental of `segment`, by **bias-corrected** autocorrelation.

    The correction is not optional, and the reason is measured (2026-10-11). The
    biased estimator - dividing by the energy at lag 0 - scales a *late* peak by
    the overlap ``(N - lag) / N``, so the shoulder of the main lobe near zero lag
    can beat the true period. On the P19 808 renders, a near-sine at E1 (41.2 Hz,
    a 1070-sample period) in a 4096-sample frame gave **0.7521 at lag 110** against
    **0.7302 at lag 1070**: the tracker reported 400.9 Hz, i.e. **+39.6 semitones**,
    for a note whose spectrum is 97 % below 60 Hz. Unbiased, the same two values
    are 0.773 and 0.988 and the true period wins. A sawtooth has a much steeper
    main lobe (P18's baseline read correctly), so the defect only appears once a
    heavily filtered sub is measured - which is exactly the 808.

    Lags longer than 60 % of the frame are outside the honest range of a short
    window and are not searched; a peak below `MIN_PEAK` is reported as **no
    measurement** (`None`), never as a value.
    """
    seg = segment - float(np.mean(segment))
    if float(np.max(np.abs(seg))) < 1e-9:
        return None
    n = len(seg)
    ac = np.correlate(seg, seg, mode="full")[n - 1 :]
    if ac[0] <= 0:
        return None
    # unbiased: the mean product over the *overlapping* part only, so a peak's
    # height no longer depends on how far into the frame it sits
    ac = ac / np.arange(n, 0, -1, dtype="float64")
    if float(ac[0]) <= 0:
        return None
    ac = ac / float(ac[0])
    lo = max(2, int(fs / fmax))
    hi = min(n - 2, int(fs / fmin), int(0.6 * n))
    if hi <= lo + 2:
        return None
    # the *shortest* period, not the tallest peak: bias correction makes the
    # peaks at one, two and three periods equal to within numerical noise, so an
    # argmax picks an arbitrary multiple (measured: 55.0 Hz read as 27.5, and
    # 82.4 Hz as 27.5, before this rule). The main-lobe shoulder near zero lag is
    # not a local maximum of the corrected curve, so the first local maximum that
    # reaches NEAR_PEAK of the tallest one - or MIN_AUTOCORR_PEAK, whichever is
    # higher - is the fundamental.
    tallest = float(np.max(ac[lo:hi]))
    threshold = max(MIN_AUTOCORR_PEAK, NEAR_PEAK * tallest)
    idx: int | None = None
    for candidate in range(lo + 1, hi - 1):
        value = float(ac[candidate])
        if value >= threshold and value >= ac[candidate - 1] and value >= ac[candidate + 1]:
            idx = candidate
            break
    if idx is None:
        return None
    if 0 < idx < len(ac) - 1:
        y0, y1, y2 = float(ac[idx - 1]), float(ac[idx]), float(ac[idx + 1])
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-12:
            idx = idx + max(-1.0, min(1.0, 0.5 * (y0 - y2) / denom))
    return fs / float(idx) if idx > 0 else None


def rms(mono: np.ndarray, fs: int, lo: float, hi: float) -> float:
    seg = mono[int(lo * fs) : int(hi * fs)]
    return float(np.sqrt(np.mean(seg**2))) if len(seg) else 0.0


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    audio, fs = sf.read(str(path), dtype="float64", always_2d=True)
    return audio.mean(axis=1), fs


def pitch_contour(mono: np.ndarray, fs: int, lo: float, hi: float, frame: int = 4096, hop: int = 128) -> list[dict]:
    """f0 over `[lo, hi)`, one row per `hop` samples.

    The frame is **4096** samples, not 2048: a 41 Hz sub needs three periods to
    be measured at all (3 x 1070 = 3210 samples), and the 2048-sample frame that
    the first version of this probe used read a 55 Hz sine as 54.29 Hz - a 1.3 %
    error that would be indistinguishable from a real detune. The cost is stated:
    the window is 93 ms, so a drop *faster* than that appears as a glissando over
    one or two rows; the drop's depth is measured with `spectral_peak_hz` instead,
    which trades frequency resolution for time resolution in the other direction.
    """
    out = []
    start = int(lo * fs)
    end = min(int(hi * fs), len(mono))
    for index in range(start, max(start + 1, end - frame), hop):
        seg = mono[index : index + frame]
        level = float(np.sqrt(np.mean(seg**2)))
        out.append({"t": round(index / fs, 4), "f0": autocorr_f0(seg, fs), "rms": level})
    return out


def spectral_peak_hz(segment: np.ndarray, fs: int, lo: float = 25.0, hi: float = 200.0) -> float | None:
    """The fundamental's frequency from a **short** frame, by parabolically
    interpolated FFT peak (Hann window: about a tenth of a bin, ~1 Hz here).

    This is the instrument for a *fast* sweep of a low note. An autocorrelation
    estimate needs three periods (73 ms at 41 Hz), and the P19 808's pitch drop is
    over in 60 ms - a window long enough to be precise is a window that smears the
    thing being measured. The band is limited to 25-200 Hz so a saw's harmonics
    cannot win: within the band the fundamental is the tallest partial for both the
    saw (P18) and the filtered sub (P19)."""
    seg = np.asarray(segment, dtype="float64")
    seg = seg - float(np.mean(seg))
    if float(np.max(np.abs(seg))) < 1e-9:
        return None
    spectrum = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    band = (freqs >= lo) & (freqs <= hi)
    if not band.any():
        return None
    masked = np.where(band, spectrum, 0.0)
    idx = int(np.argmax(masked))
    if idx <= 0 or idx >= len(spectrum) - 1 or float(spectrum[idx]) <= 0.0:
        return None
    y0, y1, y2 = float(spectrum[idx - 1]), float(spectrum[idx]), float(spectrum[idx + 1])
    denom = y0 - 2.0 * y1 + y2
    offset = 0.0 if abs(denom) <= 1e-12 else max(-1.0, min(1.0, 0.5 * (y0 - y2) / denom))
    return float(freqs[idx] + offset * float(freqs[1] - freqs[0]))


def analyse(path: Path, *, with_pitch: bool = False) -> dict[str, Any]:
    """The P18 metric set; `with_pitch` adds the first-note pitch contour."""
    mono, fs = read_mono(path)
    peak = float(np.max(np.abs(mono)))
    seg = mono[int(STEADY["n3"][0] * fs) : int(STEADY["n3"][1] * fs)]
    windowed = seg * np.hanning(len(seg))
    power = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    spectrum = np.abs(np.fft.rfft(windowed))
    total = float(power.sum()) or 1e-12

    def mag_at(f: float) -> float:
        band = (freqs >= f * 0.985) & (freqs <= f * 1.015)
        return float(spectrum[band].max()) if band.any() else 0.0

    f0 = NOTES_HZ[33]
    m1, m2, m3 = mag_at(f0), mag_at(2 * f0), mag_at(3 * f0)

    # steady pitches per note (the same windows and floor the P18 probe used)
    track = []
    for start in range(0, max(1, len(mono) - 4096), 512):
        chunk = mono[start : start + 4096]
        track.append({"t": start / fs, "rms": float(np.sqrt(np.mean(chunk**2))), "f0": autocorr_f0(chunk, fs)})
    floor = 0.05 * max((row["rms"] for row in track), default=0.0)

    def median_pitch(lo: float, hi: float) -> float | None:
        values = [row["f0"] for row in track if row["f0"] and row["rms"] >= floor and lo <= row["t"] <= hi]
        return float(np.median(values)) if values else None

    steady = {name: median_pitch(lo, hi) for name, (lo, hi) in STEADY.items()}
    steady_error = {
        name: (None if not steady[name] else round(12.0 * float(np.log2(steady[name] / NOTES_HZ[[28, 31, 33][i]])), 2))
        for i, name in enumerate(STEADY)
    }

    result: dict[str, Any] = {
        "peak_dbfs": round(20.0 * float(np.log10(max(peak, 1e-12))), 2),
        "rms_dbfs": round(20.0 * float(np.log10(max(rms(mono, fs, 0.2, 3.8), 1e-12))), 2),
        "n_clipped_samples": int(np.sum(np.abs(mono) >= 0.9999)),
        "sub_below_120": round(float(power[freqs < 120.0].sum()) / total, 4),
        "mid_150_500": round(float(power[(freqs >= 150.0) & (freqs < 500.0)].sum()) / total, 4),
        "high_above_2000": round(float(power[freqs > 2000.0].sum()) / total, 5),
        "centroid_hz": round(float((freqs * power).sum() / total), 1),
        "h2_db": None if m1 <= 0 else round(20.0 * float(np.log10(max(m2, 1e-12) / m1)), 2),
        "h3_db": None if m1 <= 0 else round(20.0 * float(np.log10(max(m3, 1e-12) / m1)), 2),
        "steady_pitch_hz": {key: (None if value is None else round(value, 2)) for key, value in steady.items()},
        "steady_offset_st": steady_error,
    }
    if with_pitch:
        contour = pitch_contour(mono, fs, *FIRST_NOTE)
        valid = [row for row in contour if row["f0"] and row["rms"] > 1e-5]
        result["contour_frame"] = 4096
        # the attack's peak pitch: the *short-window* spectral peak over the
        # first DROP_FLIGHT seconds of the note, which is where the drop happens
        start = int(FIRST_NOTE[0] * fs)
        flight = [
            spectral_peak_hz(mono[start + i : start + i + 2048], fs)
            for i in range(0, int(DROP_FLIGHT * fs), 256)
        ]
        flight = [value for value in flight if value]
        result["attack_pitch_hz"] = round(max(flight), 2) if flight else None
        result["attack_pitch_st_offset"] = (
            None if not flight else round(12.0 * float(np.log2(max(flight) / NOTES_HZ[28])), 2)
        )
        result["attack_pitch_instrument"] = "fft_peak_2048_hop256_over_first_%0.3fs" % DROP_FLIGHT
        result["peak_pitch_hz"] = round(max(row["f0"] for row in valid), 2) if valid else None
        result["peak_pitch_st_offset"] = (
            None if not valid else round(12.0 * float(np.log2(max(row["f0"] for row in valid) / NOTES_HZ[28])), 2)
        )
        result["contour"] = [{"t": row["t"], "f0": None if row["f0"] is None else round(row["f0"], 2)} for row in contour]
        # the drop's shape: when the pitch first falls within 0.5 st of the target
        steady_target = NOTES_HZ[28]
        settle = next(
            (row["t"] for row in valid if row["f0"] <= steady_target * 2 ** (0.5 / 12)),
            None,
        )
        result["settle_to_within_half_semitone_s"] = settle
        result["settle_instrument_note"] = "the contour's 93 ms window smears a faster drop"
    return result


def digest_of(values: dict[str, Any]) -> str:
    return H.text_digest(json.dumps(values, sort_keys=True, default=str))
