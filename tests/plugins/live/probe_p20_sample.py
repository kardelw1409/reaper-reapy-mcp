"""P20 instrument: measure a recorded 808 - the reference sample or a Vital render.

Read-only and offline: nothing here talks to REAPER and nothing here writes to
the files it measures (the probes hash their inputs before and after).

Every measurement names its instrument, and the instruments chosen are the ones
P19 had to correct:

- `f0` for anything slower than three periods comes from P19's pinned
  bias-corrected autocorrelation (`probe_p19_common.autocorr_f0`) - imported, not
  re-implemented, so there is one estimator in the tree;
- the *flight* of a fast pitch drop comes from a 2048-sample parabolic FFT peak
  and is a **lower bound** on the drop's start frequency (a window average);
- a harmonic level is the tallest bin within +-3 % of ``k * f0``; the noise floor
  is the median level *between* the harmonic bands, so "H2 is -22 dB" and "there
  is nothing else" stay separate statements;
- a click is a **step**: the sample-to-sample jump near the onset compared with
  the tone's own slope in the body - not the size of the first half cycle, which
  is what a fast attack of a low sine looks like anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

import probe_p19_common as C

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

#: The band a fast drop is tracked in, relative to the settled fundamental.
DROP_BAND = (0.6, 4.0)

#: Autocorrelation frame/hop for the *slow* pitch track (93 ms of smear at 4096).
SLOW_FRAME = 4096
SLOW_HOP = 1024

#: The short-window instrument's frame and hop for the drop's flight.
FLIGHT_FRAME = 2048
FLIGHT_HOP = 128


def db(value: float) -> float:
    return round(20.0 * float(np.log10(max(float(value), 1e-12))), 2)


def note_name(hz: float) -> str:
    midi = 69.0 + 12.0 * float(np.log2(hz / 440.0))
    nearest = int(round(midi))
    return f"{NOTE_NAMES[nearest % 12]}{nearest // 12 - 1}"


def cents_off(hz: float) -> float:
    midi = 69.0 + 12.0 * float(np.log2(hz / 440.0))
    return round(100.0 * (midi - round(midi)), 1)


def semitones(freq_hz: float, reference_hz: float) -> float:
    return round(12.0 * float(np.log2(freq_hz / reference_hz)), 2)


def load(path: Path) -> dict[str, Any]:
    audio, fs = sf.read(str(path), dtype="float64", always_2d=True)
    info = sf.info(str(path))
    mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    peak = float(np.max(np.abs(mono)))
    # the whole-file spectral balance, for the "closed sub vs harmonics" question
    if len(mono) > 8:
        windowed = np.fft.rfft(mono * np.hanning(len(mono)))
        freqs = np.fft.rfftfreq(len(mono), 1.0 / fs)
        power = np.abs(windowed) ** 2
        total = float(power.sum()) or 1e-12
        sub_share = round(float(power[freqs < 120.0].sum()) / total, 4)
    else:
        sub_share = None
    meta: dict[str, Any] = {
        "path": str(path),
        "samplerate": int(fs),
        "channels": int(audio.shape[1]),
        "subtype": str(info.subtype),
        "frames": int(info.frames),
        "duration_s": round(float(info.frames) / fs, 4),
        "peak_abs": round(peak, 6),
        "peak_dbfs": db(peak),
        "rms_dbfs": db(float(np.sqrt(np.mean(mono**2)))),
        "n_clipped_samples": int(np.sum(np.abs(mono) >= 0.9999)),
        "sub_below_120_share": sub_share,
        "dc_mean": round(float(np.mean(mono)), 6),
    }
    if audio.shape[1] >= 2:
        side = (audio[:, 0] - audio[:, 1]) / 2.0
        meta["side_rms_dbfs"] = db(float(np.sqrt(np.mean(side**2))))
        meta["channel_correlation"] = (
            round(float(np.corrcoef(np.asarray(audio[:, 0]), np.asarray(audio[:, 1]))[0, 1]), 4) if peak > 0 else 1.0
        )
    return {"meta": meta, "mono": mono, "fs": int(fs)}


def rms_envelope(mono: np.ndarray, fs: int, hop_s: float = 0.02, win_s: float = 0.02) -> tuple[np.ndarray, np.ndarray]:
    hop = max(1, int(round(hop_s * fs)))
    win = max(hop, int(round(win_s * fs)))
    count = max(1, 1 + max(0, len(mono) - win) // hop)
    times = np.arange(count) * hop / fs
    values = np.empty(count)
    for index in range(count):
        segment = mono[index * hop : index * hop + win]
        values[index] = float(np.sqrt(np.mean(segment**2))) if len(segment) else 0.0
    return times, values


def activity(mono: np.ndarray, fs: int, *, floor_db: float = -60.0) -> dict[str, Any]:
    peak = float(np.max(np.abs(mono)))
    threshold = max(peak * 10 ** (floor_db / 20.0), 1e-9)
    above = np.nonzero(np.abs(mono) >= threshold)[0]
    onset = int(above[0]) if len(above) else 0
    end = int(above[-1]) if len(above) else len(mono) - 1
    return {
        "threshold_db_below_peak": floor_db,
        "onset_s": round(onset / fs, 4),
        "end_s": round(end / fs, 4),
        "note_span_s": round((end - onset) / fs, 4),
        "pre_silence_s": round(onset / fs, 4),
        "post_silence_s": round((len(mono) - 1 - end) / fs, 4),
        "onset_at_file_start": onset <= 1,
    }


def attack_and_click(mono: np.ndarray, fs: int, onset: int) -> dict[str, Any]:
    """The onset: how fast the level arrives, and whether a *step* is there."""
    peak = float(np.max(np.abs(mono)))
    seg = np.abs(mono[onset:])
    running = np.maximum.accumulate(seg)

    def t_to(fraction: float) -> float | None:
        hit = np.nonzero(running >= fraction * peak)[0] if peak > 0 else np.array([], dtype=int)
        return None if not len(hit) else round(float(hit[0]) / fs, 5)

    def level_at(ms: float) -> float | None:
        index = min(len(running) - 1, int(ms / 1000.0 * fs))
        return None if peak <= 0 or index < 0 else db(float(running[index]) / peak)

    # the body's own slope, for the click comparison (notes are sustained there)
    body = mono[onset + int(0.30 * fs) : onset + int(0.60 * fs)]
    body_step = float(np.max(np.abs(np.diff(body)))) if len(body) > 1 else 0.0
    window = mono[max(0, onset - int(0.002 * fs)) : onset + int(0.030 * fs)]
    steps = np.abs(np.diff(window)) if len(window) > 1 else np.zeros(1)
    step_index = int(np.argmax(steps))
    first5 = mono[onset : onset + int(0.005 * fs)]
    if len(first5) > 8:
        power = np.abs(np.fft.rfft(first5 * np.hanning(len(first5)))) ** 2
        freqs = np.fft.rfftfreq(len(first5), 1.0 / fs)
        total = float(power.sum()) or 1e-12
        high_share = float(power[freqs > 500.0].sum()) / total
        crossings = int(np.sum(np.diff(np.signbit(first5)) != 0))
    else:
        high_share, crossings = None, None
    before = mono[max(0, onset - int(0.001 * fs)) : onset]
    after = mono[onset : onset + int(0.001 * fs)]
    return {
        "pre_onset_rms_db": db(float(np.sqrt(np.mean(before**2)))) if len(before) else None,
        "onset_sample_over_peak": None if peak <= 0 else round(float(mono[onset]) / peak, 5),
        "time_to_10pct_s": t_to(0.10),
        "time_to_50pct_s": t_to(0.50),
        "time_to_90pct_s": t_to(0.90),
        "peak_time_after_onset_s": round(float(np.argmax(seg)) / fs, 5) if len(seg) else None,
        "level_db_at": {f"{ms}ms": level_at(ms) for ms in (1, 5, 20, 50, 100)},
        "click_step_abs": round(float(steps[step_index]), 6),
        "click_step_t_s": round(float(step_index + max(0, onset - int(0.002 * fs))) / fs - onset / fs, 5),
        "click_step_over_peak": round(float(steps[step_index]) / peak, 5) if peak > 0 else None,
        "body_step_over_peak": round(body_step / peak, 5) if peak > 0 else None,
        "click_step_vs_body_step": round(float(steps[step_index]) / body_step, 2) if body_step > 0 else None,
        "dc_step_abs": round(abs(float(np.mean(after)) - float(np.mean(before))), 6) if len(before) and len(after) else None,
        "hf_ratio_first_5ms": None if high_share is None else round(high_share, 5),
        "zero_crossings_first_5ms": crossings,
    }


def pitch_measures(
    mono: np.ndarray,
    fs: int,
    onset: int,
    *,
    body_window: tuple[float, float],
    drop_window: float = 0.05,
    note_end_s: float | None = None,
    flight_span_s: float = 0.5,
) -> dict[str, Any]:
    end = len(mono) if note_end_s is None else min(int(note_end_s * fs), len(mono))
    lo_i, hi_i = int(body_window[0] * fs), min(int(body_window[1] * fs), end)
    frames = []
    for start in range(lo_i, max(lo_i + 1, hi_i - SLOW_FRAME), SLOW_HOP // 2):
        segment = mono[start : start + SLOW_FRAME]
        value = C.autocorr_f0(segment, fs, fmin=20.0, fmax=400.0)
        if value:
            frames.append({"t": round(start / fs, 4), "f0": value, "rms": float(np.sqrt(np.mean(segment**2)))})
    floor = 0.2 * max((row["rms"] for row in frames), default=0.0)
    kept = [row["f0"] for row in frames if row["rms"] >= floor]
    f0 = float(np.median(kept)) if kept else None
    result: dict[str, Any] = {
        "body_window_s": [round(body_window[0], 4), round(body_window[1], 4)],
        "body_frames_used": len(kept),
        "body_frames_total": len(frames),
        "body_f0_hz": None if f0 is None else round(f0, 3),
        "instrument_body": "autocorr_f0 (unbiased, shortest period), 4096-sample frames, hop 512",
    }
    if f0:
        result["body_note"] = note_name(f0)
        result["body_cents_off"] = cents_off(f0)
    # the drop's flight: the short-window spectral peak, band limited around f0
    if f0:
        lo_hz, hi_hz = max(20.0, f0 * DROP_BAND[0]), f0 * DROP_BAND[1]
        span = max(FLIGHT_FRAME + FLIGHT_HOP, int(min(flight_span_s, (end - onset) / fs) * fs))
        track = []
        for offset in range(0, span - FLIGHT_FRAME, FLIGHT_HOP):
            value = C.spectral_peak_hz(mono[onset + offset : onset + offset + FLIGHT_FRAME], fs, lo=lo_hz, hi=hi_hz)
            track.append({"t": round(offset / fs, 4), "hz": None if value is None else round(value, 2)})
        first = [row["hz"] for row in track if row["hz"] is not None and row["t"] <= drop_window]
        result["flight_band_hz"] = [round(lo_hz, 2), round(hi_hz, 2)]
        result["instrument_flight"] = (
            f"spectral_peak_hz over {FLIGHT_FRAME}-sample frames, hop {FLIGHT_HOP} "
            "(a window average: a LOWER bound on the drop's start)"
        )
        result["drop_start_hz"] = round(max(first), 2) if first else None
        result["drop_start_st"] = None if not first else semitones(max(first), f0)
        settle, in_tune = None, None
        for index, row in enumerate(track):
            if row["hz"] is None:
                continue
            st = semitones(row["hz"], f0)
            following = track[index : index + 5]
            if in_tune is None and abs(st) <= 0.25 and all(r["hz"] for r in following):
                in_tune = row["t"]
            if settle is None and abs(st) <= 0.5 and all(
                r["hz"] and abs(semitones(r["hz"], f0)) <= 1.0 for r in following
            ):
                settle = row["t"]
            if settle is not None and in_tune is not None:
                break
        result["settle_to_0.5st_s"] = settle
        result["first_within_0.25st_s"] = in_tune
        values = [row["hz"] for row in track if row["hz"]]
        if len(values) > 3:
            steps = np.diff(values)
            result["descending_fraction_first_flight"] = round(float(np.sum(steps < 0)) / len(steps), 3)
        result["flight_track"] = track
    # the slow track over the whole note: the honest pitch history, 93 ms smear
    slow = []
    for start in range(onset, max(onset + 1, end - SLOW_FRAME), SLOW_HOP):
        value = C.autocorr_f0(mono[start : start + SLOW_FRAME], fs, fmin=20.0, fmax=400.0)
        slow.append({"t": round(start / fs, 4), "f0": None if value is None else round(value, 2)})
    result["instrument_slow"] = f"autocorr_f0, {SLOW_FRAME}-sample frames, hop {SLOW_HOP} (93 ms smear)"
    result["slow_track"] = slow
    return result


def _band_mask(freqs: np.ndarray, target: float, *, width_fraction: float = 0.03, extra_bins: float = 1.5) -> np.ndarray:
    """The bins that count as `target`: +-`width_fraction`, but never narrower than
    `extra_bins` bins.

    Measured defect this replaces (2026-10-11, this probe): on a 0.30 s window the
    bin spacing is 3.3 Hz, the +-3 % band around 41.3 Hz is 2.5 Hz wide, and it can
    contain **no bin at all**. `level()` then returned 0.0, the base was 0, and
    every harmonic came back `None` - a *missing bin* dressed as a measurement.
    """
    step = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 1.0
    half = max(width_fraction * float(target), extra_bins * step)
    return (freqs >= float(target) - half) & (freqs <= float(target) + half)


def harmonic_measures(mono: np.ndarray, fs: int, f0: float, lo_s: float, hi_s: float, *, n_harm: int = 12) -> dict[str, Any]:
    segment = mono[int(lo_s * fs) : int(hi_s * fs)]
    if len(segment) < 2048 or f0 <= 0:
        return {"error": "body window too short or no f0", "window_s": [round(lo_s, 4), round(hi_s, 4)]}
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    freqs = np.fft.rfftfreq(len(segment), 1.0 / fs)
    power = spectrum**2
    total = float(power[(freqs >= 20.0)].sum()) or 1e-12
    bin_hz = float(freqs[1] - freqs[0]) if len(freqs) > 1 else None

    def level(hz: float) -> float:
        band = _band_mask(freqs, hz)
        return float(spectrum[band].max()) if band.any() else 0.0

    base = level(f0)
    if base <= 0:
        # never a silent zero: a harmonic table needs a fundamental bin to mean anything
        return {
            "error": "no bin within the fundamental's band in this window",
            "window_s": [round(lo_s, 4), round(hi_s, 4)],
            "f0_hz": round(f0, 3),
            "bin_hz": None if bin_hz is None else round(bin_hz, 4),
        }
    levels = [None if base <= 0 else round(20.0 * np.log10(max(level(k * f0), 1e-12) / base), 2) for k in range(1, n_harm + 1)]
    mask = np.zeros(len(freqs), dtype=bool)
    for k in range(1, n_harm + 1):
        mask |= _band_mask(freqs, k * f0)
    between = spectrum[(freqs >= 20.0) & ~mask]
    floor = float(np.median(between)) if len(between) else 0.0
    tilt = None
    if len(levels) > 2 and all(value is not None for value in levels[1:]):
        tilt = round(float(np.polyfit(range(len(levels) - 1), [float(v) for v in levels[1:]], 1)[0]), 2)
    return {
        "f0_hz": round(f0, 3),
        "window_s": [round(lo_s, 4), round(hi_s, 4)],
        "bin_hz": None if bin_hz is None else round(bin_hz, 4),
        "h_db": levels,
        "noise_floor_db": round(20.0 * np.log10(max(floor, 1e-12) / base), 2),
        "tilt_db_per_octave": tilt,
        "share_above_300hz": round(float(power[freqs > 300.0].sum()) / total, 5),
        "share_above_1khz": round(float(power[freqs > 1000.0].sum()) / total, 6),
    }


def tail_measures(mono: np.ndarray, fs: int, *, window: tuple[float, float | None] | None = None, hop_s: float = 0.02) -> dict[str, Any]:
    """The *final* descent: from the last envelope maximum to silence."""
    lo = 0.0 if window is None else window[0]
    hi = len(mono) / fs if window is None or window[1] is None else window[1]
    segment = mono[int(lo * fs) : int(hi * fs)]
    times, values = rms_envelope(segment, fs, hop_s=hop_s)
    if not len(values):
        return {"error": "empty window"}
    peak_index = int(np.argmax(values))
    levels = 20.0 * np.log10(np.maximum(values, 1e-12))
    tail = levels[peak_index:]
    local = np.nonzero((tail[1:-1] > tail[:-2]) & (tail[1:-1] >= tail[2:]))[0] + 1 if len(tail) > 2 else np.array([], dtype=int)
    last_max = int(local[-1]) if len(local) else 0
    anchor = peak_index + last_max
    reference_db = float(levels[anchor])

    def t_at(down_db: float) -> float | None:
        hit = np.nonzero(levels[anchor:] <= reference_db + down_db)[0]
        return None if not len(hit) else round(float(times[anchor + int(hit[0])]), 3)

    fit_zone = np.nonzero((levels[anchor:] <= reference_db - 3.0) & (levels[anchor:] >= reference_db - 25.0))[0]
    rate = None
    if len(fit_zone) > 4:
        rate = round(float(np.polyfit(times[anchor:][fit_zone], levels[anchor:][fit_zone], 1)[0]), 2)
    return {
        "window_s": [round(lo, 3), round(hi, 3)],
        "envelope_peak_t_s": round(float(times[peak_index]), 3),
        "final_maximum_t_s": round(float(times[anchor]), 3),
        "final_maximum_db": round(reference_db, 2),
        "t_minus_20db_s": t_at(-20.0),
        "t_minus_40db_s": t_at(-40.0),
        "t_silence_60db_s": t_at(-60.0),
        "decay_db_per_s": rate,
        "fit_zone_db": [-25.0, -3.0],
    }


def spectrum_plot(
    mono: np.ndarray, fs: int, body_window: tuple[float, float], *, points: int = 240, f_min: float = 20.0, f_max: float = 5000.0
) -> dict[str, Any]:
    """Peak-hold over log bins (the method is the point: no averaging claim)."""
    segment = mono[int(body_window[0] * fs) : int(body_window[1] * fs)]
    if len(segment) < 512:
        return {"error": "body window too short"}
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    freqs = np.fft.rfftfreq(len(segment), 1.0 / fs)
    edges = np.geomspace(f_min, min(f_max, fs / 2.0), points + 1)
    base = float(spectrum[freqs >= f_min].max()) or 1.0
    out_f, out_db = [], []
    for low, high in zip(edges[:-1], edges[1:]):
        band = (freqs >= low) & (freqs < high)
        value = float(spectrum[band].max()) if band.any() else 0.0
        out_f.append(round(float(np.sqrt(low * high)), 2))
        out_db.append(db(value / base))
    return {"freq_hz": out_f, "db_rel_max": out_db, "method": "peak-hold, log bins, body window, Hann"}


def zero_crossing_profile(mono: np.ndarray, fs: int, onset: int, *, ms: float = 40.0, max_crossings: int = 16) -> dict[str, Any]:
    """The first crossings and the half-periods between them (ms).

    This is the instrument that separated the reference's *smooth* fast drop
    (half-periods 3.36 / 4.17 / 5.06 / 6.19 ms, i.e. a whoop from ~150 Hz down)
    from the P19 render's onset (no crossing at all for the first 18 ms: the
    24 dB lowpass at 58 Hz was dragging the waveform, not passing it).
    """
    segment = mono[onset : onset + int(ms / 1000.0 * fs)]
    crossings = np.nonzero(np.diff(np.signbit(segment)))[0]
    lengths = np.diff(crossings) if len(crossings) > 1 else np.array([], dtype=int)
    return {
        "window_ms": ms,
        "crossings_ms": [round(float(value) / fs * 1000.0, 3) for value in crossings[:max_crossings]],
        "half_periods_ms": [round(float(value) / fs * 1000.0, 3) for value in lengths[:max_crossings]],
        "implied_hz": [
            round(float(fs) / (2.0 * float(value)), 1) for value in lengths[:max_crossings]
        ],
    }


def harmonic_history(mono: np.ndarray, fs: int, f0: float, windows: list[tuple[float, float]]) -> list[dict[str, Any]]:
    """h2/h3/h5 (etc.) measured in each window - how the timbre moves over the note."""
    history = []
    for low, high in windows:
        row = harmonic_measures(mono, fs, f0, low, high)
        history.append(
            {
                "window_s": [round(low, 3), round(high, 3)],
                "h_db": row.get("h_db"),
                "share_above_300hz": row.get("share_above_300hz"),
                "noise_floor_db": row.get("noise_floor_db"),
            }
        )
    return history


def envelope_points_db(times: np.ndarray, values: np.ndarray, points: tuple[float, ...], *, anchor_s: float = 0.0) -> dict[str, Any]:
    """Envelope level (dBFS) at fixed times, for an amp-envelope target."""
    out: dict[str, Any] = {}
    for point in points:
        index = int(np.argmin(np.abs(times - (anchor_s + point))))
        out[f"{point}s"] = round(float(20.0 * np.log10(max(values[index], 1e-12))), 2)
    return out


def measure(
    path: Path,
    *,
    label: str = "",
    note_window: tuple[float, float | None] | None = None,
    tail_window: tuple[float, float | None] | None = None,
    body_window: tuple[float, float] | None = None,
    drop_window: float = 0.05,
) -> dict[str, Any]:
    """Everything about one recording. `note_window` scopes the note metrics."""
    loaded = load(path)
    mono, fs, meta = loaded["mono"], loaded["fs"], loaded["meta"]
    times, envelope = rms_envelope(mono, fs)
    act = activity(mono, fs)
    note_lo = act["onset_s"] if note_window is None or note_window[0] is None else float(note_window[0])
    note_hi = None if note_window is None or note_window[1] is None else float(note_window[1])
    # the attack anchor: the first sample above the floor *inside* the note window
    peak_abs = float(np.max(np.abs(mono)))
    threshold = max(peak_abs * 10 ** (-60.0 / 20.0), 1e-9)
    search_from = max(0, int(note_lo * fs))
    above = np.nonzero(np.abs(mono[search_from:]) >= threshold)[0]
    onset = search_from + int(above[0]) if len(above) else search_from
    note_lo = round(onset / fs, 4)
    if body_window is None:
        # **inside this note**: the phrase's first note is 0.9 s long and the next
        # note starts 0.7 s after it, so a window reaching 0.8 s would measure the
        # next note's attack and glide (found 2026-10-11: it inflated h2 by 4.6 dB).
        body_window = (note_lo + 0.25, note_lo + 0.55)
    attack = attack_and_click(mono, fs, onset)
    pitch = pitch_measures(mono, fs, onset, body_window=body_window, drop_window=drop_window, note_end_s=note_hi)
    harmonics = harmonic_measures(mono, fs, float(pitch["body_f0_hz"] or 0.0), *body_window)
    tail = tail_measures(mono, fs, window=tail_window)
    plot = spectrum_plot(mono, fs, body_window)
    return {
        "label": label or str(path.name),
        "meta": meta,
        "activity": act,
        "note_window_s": [note_lo, note_hi],
        "envelope": {
            "hop_s": 0.02,
            "times_s": [round(float(value), 3) for value in times],
            "db": [round(float(20.0 * np.log10(max(value, 1e-12))), 1) for value in envelope],
        },
        "envelope_points_db": envelope_points_db(
            times, envelope, (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8), anchor_s=note_lo
        ),
        "attack": attack,
        "zero_crossings": zero_crossing_profile(mono, fs, onset),
        "pitch": pitch,
        "harmonics": harmonics,
        "tail": tail,
        "spectrum_plot": plot,
    }


# ---------------------------------------------------------------------------
# transforms used to make the comparison possible (new files only)
# ---------------------------------------------------------------------------


def resample_to_pitch(mono: np.ndarray, fs: int, f0_src: float, f0_dst: float) -> np.ndarray:
    """Playback-rate shift so `f0_src` lands on `f0_dst`: ``y[i] = x[i * f0_dst/f0_src]``.

    A step **below** 1 stretches the file (length scales by ``f0_src/f0_dst``) and
    lowers the pitch by the same factor; the sample rate is unchanged (this is not
    a re-sample to a new rate). Applied **only** to new copies for comparison and
    audition - never to the original. A feature at input time `t` sits at output
    time ``t * f0_src / f0_dst``.
    """
    step = float(f0_dst) / float(f0_src)
    positions = np.arange(0.0, len(mono) - 1.0, step)
    return np.interp(positions, np.arange(len(mono), dtype="float64"), mono)


def slice_seconds(mono: np.ndarray, fs: int, lo_s: float, hi_s: float | None) -> np.ndarray:
    lo = max(0, int(lo_s * fs))
    hi = len(mono) if hi_s is None else min(len(mono), int(hi_s * fs))
    return mono[lo:hi]


def body_rms_db(mono: np.ndarray, fs: int, lo_s: float, hi_s: float) -> float:
    return db(float(np.sqrt(np.mean(slice_seconds(mono, fs, lo_s, hi_s) ** 2))))


def write_aligned(path: Path, mono: np.ndarray, fs: int, *, target_rms_dbfs: float = -18.0, ceiling: float = 0.99) -> dict[str, Any]:
    """Write a loudness-aligned *copy* (PCM_16) without touching the original."""
    measured = db(float(np.sqrt(np.mean(mono**2)))) if len(mono) else None
    gain = 10 ** ((target_rms_dbfs - float(measured)) / 20.0) if measured is not None else 1.0
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
    limited = False
    if peak * gain > ceiling and peak > 0:
        gain = ceiling / peak
        limited = True
    out = mono * gain
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), out, fs, subtype="PCM_16")
    return {
        "file": str(path),
        "gain_db": round(20.0 * float(np.log10(max(gain, 1e-12))), 2),
        "source_rms_dbfs": measured,
        "target_rms_dbfs": target_rms_dbfs,
        "peak_after": round(float(np.max(np.abs(out))) if len(out) else 0.0, 4),
        "peak_limited": limited,
        "subtype": "PCM_16",
    }
