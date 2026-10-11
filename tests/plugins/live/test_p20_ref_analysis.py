"""P20 - the reference 808 measured, and the P19 render compared against it.

Read-only: no REAPER, no mutation, and both inputs are hashed before and after,
so "the original was not modified" is a *check* rather than a promise. Produces

- one evidence JSON holding every measurement of both recordings;
- four PNG comparisons (envelope, pitch, spectrum, attack waveform);
- loudness-aligned audition copies under new names - including the pitch-matched
  pair, which is the A/B one: the reference moved to E1, where the phrase plays.

The reference is a one-shot at F# while the phrase's first note is E1, two
semitones away. A spectrum comparison must either account for that or remove it,
and both are done: the raw rows keep the real pitches, the harmonic/spectrum and
audition comparison uses a playback-rate-shifted **copy** of the reference (a new
file; the original is never written to).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import live_harness as H
import probe_p20_sample as S

REFERENCE = Path(
    "E:/samples/download/Stickz - Future Bass Toolkit/Stickz - Future Bass Toolkit - Sample Pack/"
    "Stickz FBT - 808s/Stickz FBT - 808 10 - F#.wav"
)
PREVIOUS = H.EVIDENCE_DIR / "p19_808_patch__20261011T143226Z.wav"
#: The phrase's first note (E1) inside the P19 render, and the last note + tail.
PREV_NOTE1 = (0.18, 1.16)
PREV_TAIL = (2.58, None)
E1_HZ = 41.2034446


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row(name: str, reference: Any, previous: Any, note: str = "") -> dict[str, Any]:
    delta: Any = None
    if isinstance(reference, (int, float)) and isinstance(previous, (int, float)):
        delta = round(float(previous) - float(reference), 3)
    return {"metric": name, "reference": reference, "previous": previous, "delta_prev_minus_ref": delta, "note": note}


def test_p20_reference_measured_and_previous_render_compared():
    assert REFERENCE.is_file(), f"the reference sample is missing at {REFERENCE}"
    assert PREVIOUS.is_file(), f"the previous render is missing at {PREVIOUS}"
    ref_sha_before, prev_sha_before = _sha(REFERENCE), _sha(PREVIOUS)
    stamp = H.run_stamp()

    reference = S.measure(REFERENCE, label="reference Stickz FBT 808 10 F#")
    previous = S.measure(
        PREVIOUS, label="previous render p19_808_patch (first note E1)", note_window=PREV_NOTE1, tail_window=PREV_TAIL
    )

    # --- the pitch-matched copy: the reference played back at E1 ------------
    ref_loaded = S.load(REFERENCE)
    ref_f0 = float(reference["pitch"]["body_f0_hz"])
    stretch = ref_f0 / E1_HZ  # time positions scale by this factor
    ref_at_e1 = S.resample_to_pitch(ref_loaded["mono"], ref_loaded["fs"], ref_f0, E1_HZ)
    at_e1_path = H.EVIDENCE_DIR / f"p20_ref_as_e1_source__{stamp}.wav"
    S.write_aligned(at_e1_path, ref_at_e1, ref_loaded["fs"], target_rms_dbfs=-18.0)
    body_lo, body_hi = reference["pitch"]["body_window_s"]
    at_e1 = S.measure(
        at_e1_path,
        label="reference played back at E1",
        body_window=(body_lo * stretch, body_hi * stretch),
    )
    # how the reference's timbre moves over its 3.2 s note (body -> tail)
    ref_history = S.harmonic_history(
        ref_loaded["mono"], ref_loaded["fs"], ref_f0, [(0.30, 0.80), (1.10, 1.60), (2.00, 2.50), (2.60, 3.00)]
    )

    # --- the comparison table ----------------------------------------------
    ref_attack, prev_attack = reference["attack"], previous["attack"]
    ref_pitch, prev_pitch = reference["pitch"], previous["pitch"]
    ref_h, prev_h = reference["harmonics"], previous["harmonics"]
    ref_tail, prev_tail = reference["tail"], previous["tail"]
    rows = [
        _row("samplerate", reference["meta"]["samplerate"], previous["meta"]["samplerate"]),
        _row("channels", reference["meta"]["channels"], previous["meta"]["channels"]),
        _row("duration_s", reference["meta"]["duration_s"], previous["meta"]["duration_s"]),
        _row("peak_dbfs", reference["meta"]["peak_dbfs"], previous["meta"]["peak_dbfs"]),
        _row("rms_dbfs", reference["meta"]["rms_dbfs"], previous["meta"]["rms_dbfs"]),
        _row("note_span_s", reference["activity"]["note_span_s"], previous["activity"]["note_span_s"]),
        _row("onset_s", reference["activity"]["onset_s"], previous["activity"]["onset_s"]),
        _row("pre_silence_s", reference["activity"]["pre_silence_s"], previous["activity"]["pre_silence_s"]),
        _row("time_to_50pct_s", ref_attack["time_to_50pct_s"], prev_attack["time_to_50pct_s"]),
        _row("time_to_90pct_s", ref_attack["time_to_90pct_s"], prev_attack["time_to_90pct_s"]),
        _row("level_db_at_1ms", ref_attack["level_db_at"]["1ms"], prev_attack["level_db_at"]["1ms"]),
        _row("level_db_at_5ms", ref_attack["level_db_at"]["5ms"], prev_attack["level_db_at"]["5ms"]),
        _row("level_db_at_20ms", ref_attack["level_db_at"]["20ms"], prev_attack["level_db_at"]["20ms"]),
        _row("click_step_over_peak", ref_attack["click_step_over_peak"], prev_attack["click_step_over_peak"]),
        _row("click_step_t_s", ref_attack["click_step_t_s"], prev_attack["click_step_t_s"]),
        _row("click_step_vs_body_step", ref_attack["click_step_vs_body_step"], prev_attack["click_step_vs_body_step"]),
        _row("onset_sample_over_peak", ref_attack.get("onset_sample_over_peak"), prev_attack.get("onset_sample_over_peak")),
        _row("hf_ratio_first_5ms", ref_attack["hf_ratio_first_5ms"], prev_attack["hf_ratio_first_5ms"]),
        _row("dc_step_abs", ref_attack["dc_step_abs"], prev_attack["dc_step_abs"]),
        _row("body_f0_hz", ref_pitch["body_f0_hz"], prev_pitch["body_f0_hz"]),
        _row("body_note", ref_pitch.get("body_note"), prev_pitch.get("body_note")),
        _row("drop_start_hz", ref_pitch.get("drop_start_hz"), prev_pitch.get("drop_start_hz")),
        _row("drop_start_st", ref_pitch.get("drop_start_st"), prev_pitch.get("drop_start_st")),
        _row("settle_to_0.5st_s", ref_pitch.get("settle_to_0.5st_s"), prev_pitch.get("settle_to_0.5st_s")),
        _row("noise_floor_db", ref_h.get("noise_floor_db"), prev_h.get("noise_floor_db")),
        _row("share_above_300hz", ref_h.get("share_above_300hz"), prev_h.get("share_above_300hz")),
        _row("decay_db_per_s", ref_tail.get("decay_db_per_s"), prev_tail.get("decay_db_per_s")),
        _row("t_minus_20db_s", ref_tail.get("t_minus_20db_s"), prev_tail.get("t_minus_20db_s")),
        _row("t_minus_40db_s", ref_tail.get("t_minus_40db_s"), prev_tail.get("t_minus_40db_s")),
    ]
    for index in range(1, 9):
        rows.append(
            _row(
                f"h{index}_db",
                None if index - 1 >= len(ref_h.get("h_db") or []) else ref_h["h_db"][index - 1],
                None if index - 1 >= len(prev_h.get("h_db") or []) else prev_h["h_db"][index - 1],
                "raw pitches (F# vs E1)" if index == 1 else "",
            )
        )
    at_e1_rows = [
        _row(f"h{index}_db_at_e1", None if index - 1 >= len(at_e1["harmonics"].get("h_db") or []) else at_e1["harmonics"]["h_db"][index - 1],
             None if index - 1 >= len(prev_h.get("h_db") or []) else prev_h["h_db"][index - 1],
             "same pitch (E1): the comparable column")
        for index in range(1, 9)
    ]
    for point in ("0.02s", "0.05s", "0.1s", "0.2s", "0.4s", "0.6s", "0.8s"):
        rows.append(
            _row(
                f"envelope_db_at_{point}",
                reference["envelope_points_db"].get(point),
                previous["envelope_points_db"].get(point),
                "RMS, 20 ms window, from each note's onset",
            )
        )

    # --- plots --------------------------------------------------------------
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plots: list[str] = []
    fig, axes = plt.subplots(2, 1, figsize=(11.0, 7.0))
    for report, color in ((reference, "tab:blue"), (previous, "tab:orange")):
        times = np.array(report["envelope"]["times_s"])
        levels = np.array(report["envelope"]["db"])
        axes[0].plot(times, levels, color=color, lw=1.2, label=report["label"])
        axes[1].plot(times, levels, color=color, lw=1.2)
    axes[0].set_title("RMS envelope (20 ms windows), dBFS")
    axes[0].set_xlabel("time (s)")
    axes[0].set_ylabel("dBFS")
    axes[0].set_ylim(-70, 0)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].set_title("the onset, first 400 ms")
    axes[1].set_xlim(0, 0.4)
    axes[1].set_ylim(-70, 0)
    axes[1].set_xlabel("time (s)")
    fig.tight_layout()
    path = H.EVIDENCE_DIR / f"p20_plot_envelope__{stamp}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    plots.append(str(path))

    fig, axes = plt.subplots(2, 1, figsize=(11.0, 7.0))
    for report, color in ((reference, "tab:blue"), (previous, "tab:orange")):
        f0 = report["pitch"].get("body_f0_hz")
        if not f0:
            continue
        slow = [(row["t"], row["f0"]) for row in report["pitch"]["slow_track"] if row["f0"]]
        if slow:
            t, f = zip(*slow)
            axes[0].plot(t, [12 * np.log2(value / f0) for value in f], color=color, lw=1.2, label=report["label"])
        flight = [(row["t"], row["hz"]) for row in report["pitch"].get("flight_track", []) if row["hz"]]
        if flight:
            t, f = zip(*flight)
            axes[1].plot(t, [12 * np.log2(value / f0) for value in f], color=color, lw=1.2, label=report["label"])
    axes[0].set_title("pitch, semitones above each file's own body f0 (slow instrument, 93 ms smear)")
    axes[0].set_ylabel("semitones")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].set_title("the drop's flight (2048-sample spectral peak, first 0.5 s)")
    axes[1].set_xlabel("time from note onset (s)")
    axes[1].set_ylabel("semitones")
    axes[1].set_xlim(0, 0.5)
    fig.tight_layout()
    path = H.EVIDENCE_DIR / f"p20_plot_pitch__{stamp}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    plots.append(str(path))

    fig, ax = plt.subplots(figsize=(11.0, 5.0))
    for report, color in ((at_e1, "tab:blue"), (previous, "tab:orange")):
        freqs = np.array(report["spectrum_plot"]["freq_hz"])
        levels = np.array(report["spectrum_plot"]["db_rel_max"])
        ax.semilogx(freqs, levels, color=color, lw=1.2, label=report["label"])
    for k in range(1, 13):
        ax.axvline(E1_HZ * k, color="0.85", lw=0.6, zorder=0)
    ax.set_title("body spectrum, both at E1 (peak-hold, log bins) - grey lines are E1 harmonics")
    ax.set_xlabel("Hz")
    ax.set_ylabel("dB rel. max")
    ax.set_ylim(-80, 0)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    path = H.EVIDENCE_DIR / f"p20_plot_spectrum__{stamp}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    plots.append(str(path))

    fig, axes = plt.subplots(2, 1, figsize=(11.0, 6.0), sharex=True)
    ref_loaded_full = ref_loaded
    ref_onset = int(reference["activity"]["onset_s"] * ref_loaded_full["fs"])
    prev_loaded = S.load(PREVIOUS)
    prev_onset = int(0.20 * prev_loaded["fs"])
    span = int(0.040 * ref_loaded_full["fs"])
    ref_segment = ref_loaded_full["mono"][ref_onset : ref_onset + span]
    prev_segment = prev_loaded["mono"][prev_onset : prev_onset + span]
    ref_norm = ref_segment / (np.max(np.abs(ref_segment)) or 1.0)
    prev_norm = prev_segment / (np.max(np.abs(prev_segment)) or 1.0)
    axes[0].plot(np.arange(len(ref_norm)) / ref_loaded_full["fs"] * 1000.0, ref_norm, lw=0.9, color="tab:blue")
    axes[0].set_title("reference: first 40 ms (normalized to its own peak)")
    axes[0].set_ylabel("amplitude")
    axes[1].plot(np.arange(len(prev_norm)) / prev_loaded["fs"] * 1000.0, prev_norm, lw=0.9, color="tab:orange")
    axes[1].set_title("previous render: first 40 ms of the E1 note")
    axes[1].set_xlabel("ms after onset")
    axes[1].set_ylabel("amplitude")
    fig.tight_layout()
    path = H.EVIDENCE_DIR / f"p20_plot_attack__{stamp}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    plots.append(str(path))

    # --- audition copies (new files only) ----------------------------------
    auditions: list[dict[str, Any]] = []
    prev_loaded_mono, prev_fs = prev_loaded["mono"], prev_loaded["fs"]
    ref_copy = S.write_aligned(
        H.EVIDENCE_DIR / f"p20_ab_reference_as_e1__{stamp}.wav",
        ref_at_e1,
        ref_loaded["fs"],
        target_rms_dbfs=-18.0,
    )
    auditions.append(ref_copy)
    auditions.append(
        S.write_aligned(
            H.EVIDENCE_DIR / f"p20_ab_previous_note1__{stamp}.wav",
            S.slice_seconds(prev_loaded_mono, prev_fs, *PREV_NOTE1),
            prev_fs,
            target_rms_dbfs=-18.0,
        )
    )
    auditions.append(
        S.write_aligned(
            H.EVIDENCE_DIR / f"p20_ab_previous_lastnote__{stamp}.wav",
            S.slice_seconds(prev_loaded_mono, prev_fs, 2.58, None),
            prev_fs,
            target_rms_dbfs=-18.0,
        )
    )

    ref_sha_after, prev_sha_after = _sha(REFERENCE), _sha(PREVIOUS)
    report = {
        "probe": "p20_reference_analysis",
        "utc": H.utc_now(),
        "read_only": True,
        "inputs": {
            "reference": str(REFERENCE),
            "previous_render": str(PREVIOUS),
            "sha256_before": {"reference": ref_sha_before, "previous": prev_sha_before},
            "sha256_after": {"reference": ref_sha_after, "previous": prev_sha_after},
            "unchanged": ref_sha_before == ref_sha_after and prev_sha_before == prev_sha_after,
            "pitch_shift": {
                "reference_f0_hz": ref_f0,
                "target_hz": E1_HZ,
                "time_stretch": round(stretch, 5),
                "method": "linear-interpolation playback-rate shift into a NEW file; the original is not written",
            },
        },
        "reference": reference,
        "previous": previous,
        "reference_at_e1": at_e1,
        "reference_harmonic_history": ref_history,
        "comparison_rows": rows,
        "comparison_rows_at_e1": at_e1_rows,
        "plots": plots,
        "auditions": auditions,
    }
    path = H.write_run_json("p20_reference_analysis", report, H.EVIDENCE_DIR)
    print(f"evidence: {path}")
    for row in rows + at_e1_rows:
        print(f"  {row['metric']:<26} ref={row['reference']!s:<12} prev={row['previous']!s:<12} delta={row['delta_prev_minus_ref']}")
    print(f"reference note: {ref_pitch.get('body_note')} ({ref_f0} Hz, {ref_pitch.get('body_cents_off')} cents)")
    print(f"reference drop: start {ref_pitch.get('drop_start_hz')} Hz = {ref_pitch.get('drop_start_st')} st, settle {ref_pitch.get('settle_to_0.5st_s')} s")
    print(f"previous  drop: start {prev_pitch.get('drop_start_hz')} Hz = {prev_pitch.get('drop_start_st')} st, settle {prev_pitch.get('settle_to_0.5st_s')} s")
    print(f"reference timbre over the note: {json.dumps(ref_history)}")
    print(f"reference envelope points: {json.dumps(reference['envelope_points_db'])}")
    print(f"previous  envelope points: {json.dumps(previous['envelope_points_db'])}")
    print("plots:")
    for entry in plots:
        print(f"  {entry}")
    print("auditions:")
    for entry in auditions:
        print(f"  {entry['file']}  gain {entry['gain_db']} dB  peak {entry['peak_after']}")

    assert report["inputs"]["unchanged"] is True, report["inputs"]
    assert str(ref_pitch.get("body_note", "")).startswith("F#"), ref_pitch
    assert str(prev_pitch.get("body_note", "")) == "E1", prev_pitch
    for entry in auditions:
        assert Path(entry["file"]).is_file() and Path(entry["file"]).stat().st_size > 1000, entry
    for entry in plots:
        assert Path(entry).is_file(), entry
