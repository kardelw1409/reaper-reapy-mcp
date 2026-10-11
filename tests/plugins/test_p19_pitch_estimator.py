"""P19 — the pitch estimator, pinned against the case that broke it (offline).

**Why this file exists.** The live P19 808 arms were rendered, and the harness's
`autocorr_f0` reported **+39.55 semitones** for their first note (E1, 41.2 Hz) -
while the same render's spectrum puts 97 % of its energy below 60 Hz and the
fundamental at 42 Hz. The cause was the *estimator*: dividing the autocorrelation
by the energy at lag 0 scales a late peak by the overlap `(N - lag) / N`, so the
shoulder of the main lobe near zero lag beat the true period. Measured on the
render's own 4096-sample chunk: biased `0.7521` at lag 110 vs `0.7302` at lag
1070 (the true period); unbiased, `0.773` vs `0.988`.

A sawtooth has a steeper main lobe, and the P18/P19 baseline renders (a saw) read
correctly - so the defect stayed invisible until a *heavily filtered sub* was
measured, i.e. until the very sound this phase exists to build. The test below
therefore pins the **correct** behaviour on synthetic signals at the exact frame
lengths and fundamental frequencies the live probes use, and pins the *counter-
example* (a near-sine whose main-lobe shoulder is flat) as the case that must not
regress.
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pytest

# the shared probe helpers live beside the live suites; this file is offline, so
# it asks for them explicitly instead of relying on a live-suite conftest
_HERE = pathlib.Path(__file__).resolve().parent
for _path in (str(_HERE), str(_HERE / "live")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import probe_p19_common as C  # noqa: E402 - path set up above, deliberately

FS = 44100
E1_HZ = 41.2034446  # the phrase's lowest note
A1_HZ = 55.0


def saw(f0: float, seconds: float, *, harmonics: int = 40, sample_rate: int = FS) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    out = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        out += np.sin(2.0 * np.pi * k * f0 * t) / k
    return out


def lowpass(x: np.ndarray, cutoff: float, sample_rate: int = FS) -> np.ndarray:
    """Zero-phase brickwall low pass (FFT), so the result is *stationary* and
    exactly periodic: a one-pole filter's start-up transient inside the frame is
    not what a rendered note looks like, and it would make the test measure the
    stimulus rather than the estimator."""
    spectrum = np.fft.rfft(np.asarray(x, dtype="float64"))
    freqs = np.fft.rfftfreq(len(spectrum) * 2 - 2, 1.0 / sample_rate)
    spectrum[freqs > cutoff] = 0.0
    return np.fft.irfft(spectrum, n=len(x))


def steady_frame(f0: float, frame: int, *, cutoff_factor: float = 1.6) -> np.ndarray:
    """`frame` samples of a filtered sawtooth taken from the *middle* of a longer,
    settled signal (both frame lengths the live probes use are cut this way)."""
    signal = lowpass(saw(f0, 0.5), cutoff_factor * f0)
    start = (len(signal) - frame) // 2
    return signal[start : start + frame]


def test_the_estimator_reads_a_filtered_sub_correctly_at_the_live_frame_length():
    """The measured failure: 4096 samples, a near-sine at E1, biased estimator wrong."""
    frame = steady_frame(E1_HZ, 4096, cutoff_factor=1.45)  # ~60 Hz cutoff, as the 808 patch writes
    # the spectrum really is the sub (the assertion the estimator contradicted)
    spectrum = np.abs(np.fft.rfft(frame * np.hanning(len(frame))))
    freqs = np.fft.rfftfreq(len(frame), 1.0 / FS)
    below_60 = float((spectrum[freqs < 60.0] ** 2).sum() / (spectrum**2).sum())
    assert below_60 > 0.9, below_60
    measured = C.autocorr_f0(frame, FS)
    assert measured is not None
    assert measured == pytest.approx(E1_HZ, rel=0.01), measured


@pytest.mark.parametrize("f0", [E1_HZ, A1_HZ, 82.4068892, 110.0])
def test_low_fundamentals_survive_the_frame_the_probes_use(f0):
    """The 4096-sample frame (the contour's and the steady tracker's) must read a
    low fundamental within 1 %."""
    measured = C.autocorr_f0(steady_frame(f0, 4096), FS)
    assert measured is not None, f0
    assert measured == pytest.approx(f0, rel=0.01), (f0, measured)


@pytest.mark.parametrize("f0", [E1_HZ, A1_HZ])
def test_a_2048_sample_frame_is_measurably_below_the_resolution_limit(f0):
    """**The limit, measured rather than assumed.** A 2048-sample frame holds 1.9
    periods of E1 and 2.5 of A1; the estimate that comes out is wrong by more than
    1 % - 55 Hz read as 54.29 Hz, which is indistinguishable from a real detune.
    No probe may use a 2048-sample frame on this material; if a future estimator
    beats this, the test should be updated *deliberately*."""
    measured = C.autocorr_f0(steady_frame(f0, 2048), FS)
    assert measured is not None
    assert abs(measured - f0) / f0 > 0.01, (f0, measured)


def test_a_sawtooth_still_reads_correctly_the_case_that_always_worked():
    """P18's baseline renders were a saw and read correctly: the correction must
    not trade one shape for another."""
    for frame in (2048, 4096):
        measured = C.autocorr_f0(saw(E1_HZ, frame / FS + 0.05)[:frame], FS)
        assert measured is not None
        assert measured == pytest.approx(E1_HZ, rel=0.01), (frame, measured)


def test_the_bias_corrected_estimator_disagrees_with_the_biased_one_where_it_matters():
    """The counter-example, kept as a *measurement* rather than as prose: on this
    shape the biased peak near lag 110 is higher than the true period's, and the
    corrected one is not. If either half stops holding, the reason for the
    correction has changed and this file must be revisited."""
    frame = steady_frame(E1_HZ, 4096, cutoff_factor=1.45)
    seg = frame - float(np.mean(frame))
    raw = np.correlate(seg, seg, mode="full")[len(seg) - 1 :]
    biased = raw / raw[0]
    unbiased = raw / np.arange(len(seg), 0, -1, dtype="float64")
    unbiased = unbiased / unbiased[0]
    true_lag = int(round(FS / E1_HZ))
    shoulder = 110  # fs / fmax, the first lag the search may return
    assert biased[shoulder] > biased[true_lag], (biased[shoulder], biased[true_lag])
    assert unbiased[shoulder] < unbiased[true_lag], (unbiased[shoulder], unbiased[true_lag])


def test_silence_and_weak_periodicity_are_no_measurement_not_a_number():
    """`None` is the absence of a measurement; a value is a claim."""
    assert C.autocorr_f0(np.zeros(4096), FS) is None
    rng = np.random.default_rng(19)
    noise = rng.normal(0.0, 1.0, 4096)
    measured = C.autocorr_f0(noise, FS)
    if measured is not None:  # a lucky noise peak is allowed, but it must clear the floor
        assert measured > 0.0
    assert C.spectral_peak_hz(np.zeros(2048), FS) is None


def test_a_second_harmonic_dominant_signal_resolves_an_octave_high_an_accepted_limit():
    """**An accepted limitation, pinned as a measurement** (an independent review of
    2026-10-11 constructed it): if the second harmonic is much louder than the
    fundamental, the corrected curve's peak at half the period reaches the
    near-peak threshold and the estimator reports the octave above.

    It cannot occur in the materials this project measures - a low-passed saw and a
    raw saw are both fundamental-dominant (the sub's h2 is at least 16 dB down,
    measured per note in `p19_acceptance_detail__*`) - so the honest response is to
    keep the case visible rather than to widen the rule for a signal nobody renders.
    """
    t = np.arange(4096) / FS
    fundamental = E1_HZ
    signal = 0.2 * np.sin(2.0 * np.pi * fundamental * t) + 1.0 * np.sin(2.0 * np.pi * 2 * fundamental * t)
    measured = C.autocorr_f0(signal, FS)
    assert measured is not None
    assert measured == pytest.approx(2 * fundamental, rel=0.01), measured
    # and the case that *is* rendered reads correctly: h2 16 dB down
    sub = 1.0 * np.sin(2.0 * np.pi * fundamental * t) + 0.16 * np.sin(2.0 * np.pi * 2 * fundamental * t)
    assert C.autocorr_f0(sub, FS) == pytest.approx(fundamental, rel=0.01)


def test_the_short_window_instrument_reads_a_fast_drop_that_a_long_window_smears():
    """The two instruments answer different questions, and the drop needs the short
    one: an autocorrelation window long enough to measure 41 Hz (73 ms) cannot see a
    60 ms sweep, while the FFT-peak instrument follows it.

    **And the short one is itself bounded, measured here rather than assumed:** a
    2048-sample window is 46 ms, so over a 60 ms flight it reports the *window
    average*, 53.6 Hz for a sweep from 61.7 Hz to 41.2 Hz. The honest claim from
    this instrument is therefore "the attack is at least this high", never "the
    note starts at X Hz" - the instantaneous start of a sub-frequency sweep shorter
    than the window is not observable by any windowed method.
    """
    fs = FS
    onset, flight, settle = 0.20, 0.06, E1_HZ * 2 ** (7.0 / 12)
    seconds = 0.5
    t = np.arange(int(seconds * fs)) / fs
    elapsed = np.clip(t - onset, 0.0, None)  # the flight starts *at* the note
    instantaneous = np.where(
        (t >= onset) & (elapsed < flight),
        settle + (E1_HZ - settle) * (elapsed / flight),
        E1_HZ,
    )
    phase = 2.0 * np.pi * np.cumsum(instantaneous) / fs
    signal = np.sin(phase)
    start = int(onset * fs)
    flight_values = [
        C.spectral_peak_hz(signal[start + i : start + i + 2048], fs)
        for i in range(0, int(C.DROP_FLIGHT * fs), 256)
    ]
    flight_values = [value for value in flight_values if value]
    assert flight_values, "the short instrument produced no reading at all"
    top = max(flight_values)
    # a lower bound the instrument can support: clearly off base, and below the
    # sweep's true start because the window averages the flight
    assert top > E1_HZ * 2 ** (3.0 / 12), top
    assert top < settle, (top, settle)
    # the long instrument sees the flight only as a *fraction* of itself: 47.7 Hz
    # for a sweep that starts at 61.7 Hz, because its 93 ms window is longer than
    # the flight - so a contour can show a drop's presence, not its depth
    contour = C.pitch_contour(signal, fs, onset, onset + 0.25, hop=128)
    first = next(row["f0"] for row in contour if row["f0"])
    assert E1_HZ * 1.02 < first < settle * 0.85, first
