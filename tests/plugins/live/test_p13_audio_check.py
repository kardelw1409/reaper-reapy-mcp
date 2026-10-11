"""P13 audio check: the 808 patch is measured **as sound**, not only as values.

Run after `test_p13_bass_acceptance.py`. It answers the next question honestly:
does what the instance now plays match the job - a deep monophonic sub with
audible pitch slides, controlled upper harmonics, a definite attack and a
controlled tail?

How, using only means the existing REAPER MCP already offers:

- the patch is **re-read through the registered V3 `plugin_read`** first, so the
  audition is of the sound the acceptance run left (a different patch is a skip,
  not a failure of the framework);
- a **temporary** MIDI item with an overlapping-note phrase is created on the
  Vital track through the classic tools, inside a **free window** that is derived
  from the project's own item list (nothing existing is touched);
- the classic `render_project` tool renders that window; its render-settings side
  effects are **snapshotted and restored** around the call (the tool restores the
  time selection itself, not the RENDER_* settings);
- the audio is analysed numerically: pitch trajectory (autocorrelation, so the
  glide is a measurement and not a parameter value), harmonic ratio, high-band
  fraction, attack rise, tail decay, clipping;
- the temporary item is **deleted** and its absence verified; the project is
  never saved, closed or switched.

What this cannot do: nobody *listens* here. The rendered WAV is kept beside the
artifact so the sound can be auditioned by a person; this probe's claims are
about measured signal properties, and the report says so.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import numpy as np
import pytest
import soundfile as sf

import reapy
from reapy import reascript_api as RPR

import live_harness as H

pytestmark = pytest.mark.live_write

#: The patch this audition is valid for (the acceptance run's target values).
PATCH = {
    "filter_1_on": "On",
    "distortion_on": "On",
    "legato": "On",
    "polyphony": 1.0,
}
PORTAMENTO_NORM_EXPECTED = 0.466148
ENV_ATTACK_NATIVE_EXPECTED = 0.25

#: The phrase, relative to the item start (seconds). Overlaps are the glide
#: trigger; the gap before `note 4` exercises the forced glide.
PHRASE = [
    {"pitch": 28, "start": 0.2, "length": 0.9, "velocity": 96},  # E1 41.20 Hz
    {"pitch": 31, "start": 0.9, "length": 0.9, "velocity": 96},  # G1 49.00 Hz, overlaps n1
    {"pitch": 33, "start": 1.6, "length": 0.8, "velocity": 96},  # A1 55.00 Hz, overlaps n2
    {"pitch": 28, "start": 2.6, "length": 1.0, "velocity": 96},  # back down (forced glide)
]
ITEM_LENGTH = 4.0
NOTES_HZ = {28: 41.2034446, 31: 48.9994295, 33: 54.9999999}
STEADY = {"n1": (0.30, 0.80), "n2": (1.30, 1.55), "n3": (1.90, 2.30)}
GLIDES = {"up_1": (0.92, 1.10), "up_2": (1.62, 1.80), "down": (2.62, 2.80)}
TAIL = (3.85, 4.00)
ATTACK_WINDOW = (0.18, 0.50)

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
    """Call a registered tool and read its payload, whatever shape it answers.

    FastMCP answers a **blocks list** for the plugin tools and a
    ``(blocks, structured)`` tuple for the classic tools (measured here); a
    probe that assumed one shape failed on the other.
    """
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


def _autocorr_f0(segment: np.ndarray, fs: int, fmin: float = 25.0, fmax: float = 200.0) -> float | None:
    """Fundamental of one frame by normalised autocorrelation + parabolic fit."""
    seg = segment - float(np.mean(segment))
    if float(np.max(np.abs(seg))) < 1e-9:
        return None
    ac = np.correlate(seg, seg, mode="full")[len(seg) - 1 :]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    lo = max(2, int(fs / fmax))
    hi = min(len(ac) - 2, int(fs / fmin))
    if hi <= lo:
        return None
    idx = lo + int(np.argmax(ac[lo:hi]))
    if 0 < idx < len(ac) - 1:
        y0, y1, y2 = float(ac[idx - 1]), float(ac[idx]), float(ac[idx + 1])
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-12:
            idx = idx + max(-1.0, min(1.0, 0.5 * (y0 - y2) / denom))
    return fs / float(idx) if idx > 0 else None


def _pitch_track(mono: np.ndarray, fs: int, frame: int = 4096, hop: int = 512) -> list[dict[str, Any]]:
    track = []
    for start in range(0, max(1, len(mono) - frame), hop):
        seg = mono[start : start + frame]
        track.append(
            {
                "t": round(start / fs, 4),
                "rms": round(float(np.sqrt(np.mean(seg**2))), 6),
                "f0": _autocorr_f0(seg, fs),
            }
        )
    return track


def _median_pitch(track: list[dict[str, Any]], lo: float, hi: float, floor: float) -> float | None:
    values = [
        row["f0"]
        for row in track
        if row["f0"] is not None and row["rms"] >= floor and lo <= row["t"] <= hi
    ]
    return float(np.median(values)) if values else None


def _band_fractions(mono: np.ndarray, fs: int, lo: float, hi: float) -> dict[str, float]:
    seg = mono[int(lo * fs) : int(hi * fs)]
    windowed = seg * np.hanning(len(seg))
    power = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    total = float(power.sum()) or 1e-12
    return {
        "sub_below_120": float(power[freqs < 120.0].sum()) / total,
        "high_above_2000": float(power[freqs > 2000.0].sum()) / total,
        "total_power": total,
    }


def _harmonic_ratio_db(mono: np.ndarray, fs: int, lo: float, hi: float, f0: float) -> dict[str, Any]:
    seg = mono[int(lo * fs) : int(hi * fs)]
    windowed = seg * np.hanning(len(seg))
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)

    def mag_at(f: float) -> float:
        band = (freqs >= f * 0.985) & (freqs <= f * 1.015)
        return float(spectrum[band].max()) if band.any() else 0.0

    m1, m2, m3 = mag_at(f0), mag_at(2 * f0), mag_at(3 * f0)
    return {
        "f0_hz": f0,
        "mag_f0": m1,
        "mag_2f0": m2,
        "mag_3f0": m3,
        "h2_db": None if m1 <= 0 else round(20.0 * float(np.log10(max(m2, 1e-12) / m1)), 2),
        "h3_db": None if m1 <= 0 else round(20.0 * float(np.log10(max(m3, 1e-12) / m1)), 2),
    }


def _rms(mono: np.ndarray, fs: int, lo: float, hi: float) -> float:
    seg = mono[int(lo * fs) : int(hi * fs)]
    return float(np.sqrt(np.mean(seg**2))) if len(seg) else 0.0


def _attack_rise_ms(mono: np.ndarray, fs: int, window: tuple[float, float], reference: float) -> float | None:
    """Time from 10 % to 90 % of the note's own steady level, in milliseconds."""
    lo, hi = int(window[0] * fs), int(window[1] * fs)
    step = 64
    envelope = [
        float(np.sqrt(np.mean(mono[i : i + step] ** 2)))
        for i in range(lo, min(hi, len(mono) - step), step)
    ]
    if not envelope or reference <= 0:
        return None
    times = [(lo + i * step) / fs for i in range(len(envelope))]
    t10 = next((t for t, e in zip(times, envelope) if e >= 0.10 * reference), None)
    t90 = next((t for t, e in zip(times, envelope) if e >= 0.90 * reference), None)
    return None if t10 is None or t90 is None else round(1000.0 * (t90 - t10), 2)


def test_the_patch_is_measured_as_sound():
    try:
        project = reapy.Project()
        project.id  # noqa: B018 - touching the project is the reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the audio check did not run: {exc}")

    # the registered tools resolve their provider from the environment on call
    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)

    report: dict[str, Any] = {
        "probe": "p13_audio_check",
        "utc": H.utc_now(),
        "live_write": False,
        "brief": (
            "temporary MIDI audition of the accepted patch: rendered, measured, removed; "
            "the project is never saved"
        ),
        "calls": [],
        "patch_check": {},
        "window": {},
        "item": {},
        "render": {},
        "analysis": {},
        "cleanup": {},
        "evidence": None,
    }

    def called(name: str, result: dict) -> None:
        report["calls"].append({"tool": name, "status": result.get("status") or ("ok" if result.get("ok") else "error")})

    # ---- 1. the audition is of the accepted patch, re-read through the tool ----
    instance = {"track_index": 0, "fx_index": 0}
    read = call(server, "plugin_read", {"instance": instance, "controls": sorted(PATCH) + ["portamento_time", "env_1_attack"]})
    called("plugin_read(patch)", read)
    values = (read.get("data") or {}).get("values") or {}
    report["patch_check"] = values
    problems: list[str] = []
    for control_id, expected in PATCH.items():
        observed = (values.get(control_id) or {}).get("display")
        observed_native = (values.get(control_id) or {}).get("native")
        if control_id == "polyphony":
            if observed_native != expected:
                problems.append(f"{control_id}: {observed_native!r} != {expected!r}")
        elif str(observed) != str(expected):
            problems.append(f"{control_id}: {observed!r} != {expected!r}")
    portamento_norm = (values.get("portamento_time") or {}).get("normalized")
    if portamento_norm is None or abs(float(portamento_norm) - PORTAMENTO_NORM_EXPECTED) > 0.003:
        problems.append(f"portamento_time: {portamento_norm!r} is not the accepted glide")
    attack_native = (values.get("env_1_attack") or {}).get("native")
    if attack_native is None or abs(float(attack_native) - ENV_ATTACK_NATIVE_EXPECTED) > 0.01:
        problems.append(f"env_1_attack: {attack_native!r} is not the accepted attack")
    if problems:
        pytest.skip(
            "the P13 808 patch is not present in the instance; run "
            f"test_p13_bass_acceptance.py first (differences: {problems})"
        )

    # ---- 2. the project's own inventory decides where a temporary item may go ---
    track_count = call(server, "get_track_count", {})
    called("get_track_count", track_count)
    n_tracks = int(track_count.get("track_count") or 0)
    intervals: list[tuple[float, float]] = []
    items_by_track: dict[int, Any] = {}
    for track_index in range(n_tracks):
        found = call(server, "get_items_in_time_range", {"track_index": track_index, "start_time": 0.0, "end_time": 900.0})
        called("get_items_in_time_range", found)
        items_by_track[track_index] = found.get("items") or []
        for item in items_by_track[track_index]:
            intervals.append((float(item["position"]), float(item["position"]) + float(item["length"])))
    track_flags = {}
    for track_index, track in enumerate(project.tracks):
        track_id = track.id
        track_flags[track_index] = {
            "name": track.name,
            "mute": float(RPR.GetMediaTrackInfo_Value(track_id, "B_MUTE")),
            "solo": float(RPR.GetMediaTrackInfo_Value(track_id, "I_SOLO")),
        }
    window_start = round(max((end for _start, end in intervals), default=0.0) + 0.5, 3)
    report["window"] = {
        "n_tracks": n_tracks,
        "items_by_track": {str(k): len(v) for k, v in items_by_track.items()},
        "intervals_end_max": max((end for _start, end in intervals), default=0.0),
        "track_flags": track_flags,
        "window_start": window_start,
        "window_end": round(window_start + ITEM_LENGTH, 3),
    }
    assert n_tracks >= 1, "the Vital track must exist"
    assert all(flag["solo"] == 0.0 for flag in track_flags.values()), (
        f"a soloed track would hide the Vital track in the render: {track_flags}"
    )
    assert track_flags.get(0, {}).get("mute", 0.0) == 0.0, "the Vital track is muted; a render would be silent"

    # ---- 3. the temporary phrase ------------------------------------------------
    created = call(
        server,
        "create_midi_item",
        {"track_index": 0, "start_time": window_start, "length_time": ITEM_LENGTH},
    )
    called("create_midi_item", created)
    report["item"] = {"created": created, "phrase": PHRASE, "window_start": window_start}
    assert created.get("status") == "success", f"the temporary item was not created: {created}"
    item_id = created.get("direct_item_id")

    pid = project.id
    int_before = {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in RENDER_INT_KEYS}
    str_before = {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in RENDER_STR_KEYS}
    selection_before = (project.time_selection.start, project.time_selection.end)
    out_path = H.EVIDENCE_DIR / f"p13_bass_render__{H.run_stamp()}.wav"
    rendered: dict[str, Any] = {}
    settings_error: str | None = None

    # one try/finally covers everything the temporary item exists for: the P12
    # lesson - the way out of a mutation belongs in `finally`, because a probe
    # defect must not leave material behind in someone's project
    try:
        notes = [
            {
                "pitch": note["pitch"],
                "start_time": round(window_start + note["start"], 4),
                "length_time": note["length"],
                "velocity": note["velocity"],
            }
            for note in PHRASE
        ]
        added = call(server, "add_midi_notes", {"track_index": 0, "item": item_id, "notes": notes})
        called("add_midi_notes", added)
        report["item"]["notes_added"] = added
        assert added.get("status") == "success", f"the phrase was not added: {added}"

        listed = call(server, "get_midi_notes", {"track_index": 0, "item_id": item_id})
        called("get_midi_notes", listed)
        report["item"]["notes_listed"] = listed.get("notes")
        assert int(listed.get("count") or 0) == len(PHRASE), "the item must carry exactly the phrase"

        rendered = call(
            server,
            "render_project",
            {
                "output_file": str(out_path),
                "start_time": window_start,
                "end_time": window_start + ITEM_LENGTH,
                "samplerate": 44100,
                "channels": 2,
                "kwargs": {},
            },
        )
        called("render_project", rendered)
    finally:
        # restore the render settings this tool sets; it restores the time selection itself
        try:
            for key, value in int_before.items():
                RPR.GetSetProjectInfo(pid, key, value, True)
            for key, value in str_before.items():
                RPR.GetSetProjectInfo_String(pid, key, value, True)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
            settings_error = f"{type(exc).__name__}: {exc}"
        # the temporary item goes away here whatever happened above
        try:
            deleted = call(server, "delete_item", {"track_index": 0, "item": item_id})
            called("delete_item", deleted)
            remaining = call(
                server,
                "get_items_in_time_range",
                {"track_index": 0, "start_time": 0.0, "end_time": 900.0},
            )
            called("get_items_in_time_range(after)", remaining)
        except Exception as exc:  # noqa: BLE001 - recorded; the assertions below still check
            deleted = {"status": f"cleanup_error: {type(exc).__name__}: {exc}"}
            remaining = {"items": None}
        report["cleanup"] = {
            "delete_result": deleted,
            "items_after": remaining.get("items"),
            "window_empty": not any(
                float(item["position"]) < window_start + ITEM_LENGTH
                and float(item["position"]) + float(item["length"]) > window_start
                for item in (remaining.get("items") or [])
            ),
        }

    int_after = {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in RENDER_INT_KEYS}
    str_after = {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in RENDER_STR_KEYS}
    selection_after = (project.time_selection.start, project.time_selection.end)
    report["render"] = {
        "output_file": str(out_path),
        "result": rendered,
        "settings_before": {**int_before, **str_before},
        "settings_after": {**int_after, **str_after},
        "settings_restored": bool(int_after == int_before and str_after == str_before),
        "settings_error": settings_error,
        "time_selection_before": selection_before,
        "time_selection_after": selection_after,
        "time_selection_restored": bool(selection_after == selection_before),
    }
    assert rendered.get("status") == "success", f"the render failed: {rendered}"
    assert out_path.is_file() and out_path.stat().st_size > 100_000, "the render file must exist and be real"

    # ---- 5. the audio, measured ---------------------------------------------------
    audio, fs = sf.read(str(out_path), dtype="float64", always_2d=True)
    mono = audio.mean(axis=1)
    duration = len(mono) / fs
    track = _pitch_track(mono, fs)
    peak = float(np.max(np.abs(mono)))
    floor = 0.05 * max((row["rms"] for row in track), default=0.0)
    steady_level = max(_rms(mono, fs, *STEADY["n3"]), 1e-9)
    analysis: dict[str, Any] = {
        "seconds": round(duration, 4),
        "samplerate": fs,
        "peak_dbfs": round(20.0 * float(np.log10(max(peak, 1e-12))), 2),
        "rms_dbfs": round(20.0 * float(np.log10(max(_rms(mono, fs, 0.2, 3.8), 1e-12))), 2),
        "n_clipped_samples": int(np.sum(np.abs(mono) >= 0.9999)),
        "steady_pitch_hz": {
            name: _median_pitch(track, lo, hi, floor) for name, (lo, hi) in STEADY.items()
        },
        "expected_hz": {name: NOTES_HZ[PHRASE[index]["pitch"]] for index, name in enumerate(STEADY)},
        "bands_n3": _band_fractions(mono, fs, *STEADY["n3"]),
        "harmonics_n3": _harmonic_ratio_db(mono, fs, *STEADY["n3"], NOTES_HZ[33]),
        "attack": {
            "rise_ms": _attack_rise_ms(mono, fs, ATTACK_WINDOW, _rms(mono, fs, *STEADY["n1"])),
            "steady_rms": _rms(mono, fs, *STEADY["n1"]),
        },
        "tail": {
            "rms": _rms(mono, fs, *TAIL),
            "ratio_vs_n3": _rms(mono, fs, *TAIL) / steady_level,
        },
        "glides": {},
        "pitch_track": [
            {"t": row["t"], "f0": None if row["f0"] is None else round(row["f0"], 2)}
            for row in track
            if row["rms"] >= floor
        ],
    }
    for name, (lo, hi) in GLIDES.items():
        pitches = [
            row["f0"]
            for row in track
            if row["f0"] is not None and row["rms"] >= floor and lo <= row["t"] <= hi
        ]
        origin, target = (41.2034446, 48.9994295) if name.startswith("up") else (54.9999999, 41.2034446)
        intermediate = [
            value for value in pitches if min(origin, target) * 1.02 < value < max(origin, target) * 0.98
        ]
        analysis["glides"][name] = {
            "window": [lo, hi],
            "n_frames": len(pitches),
            "first": None if not pitches else round(pitches[0], 2),
            "last": None if not pitches else round(pitches[-1], 2),
            "intermediate_frames": len(intermediate),
            "span_hz": None if len(pitches) < 2 else round(max(pitches) - min(pitches), 2),
        }
    report["analysis"] = analysis

    # ---- 6. the temporary material was already removed in the `finally` above ----
    report["cleanup"]["note"] = "deletion runs in the finally that covers the phrase and the render"

    path = H.write_run_json("p13_audio_check", report, H.EVIDENCE_DIR)
    report["evidence"] = str(path)
    print(f"\n[p13 audio] evidence: {path}")
    print(f"[p13 audio] render:   {out_path}")

    # ---- assertions last -----------------------------------------------------------
    assert report["cleanup"]["delete_result"].get("status") == "success", report["cleanup"]
    assert report["cleanup"]["window_empty"], "the temporary item must be gone from the window"
    assert report["render"]["settings_restored"], report["render"]
    assert report["render"]["time_selection_restored"], report["render"]
    assert duration >= ITEM_LENGTH - 0.05, "the render must cover the whole phrase"
    assert analysis["peak_dbfs"] > -40.0, "the render is silent; the audition proves nothing"
    assert analysis["n_clipped_samples"] == 0, "the patch clips; a bass that clips was not controlled"
    steady = analysis["steady_pitch_hz"]
    assert steady["n1"] is not None and abs(steady["n1"] - NOTES_HZ[28]) / NOTES_HZ[28] < 0.03, steady
    assert steady["n3"] is not None and abs(steady["n3"] - NOTES_HZ[33]) / NOTES_HZ[33] < 0.03, steady
    for name in ("up_1", "up_2"):
        assert analysis["glides"][name]["intermediate_frames"] >= 3, (
            f"the {name} slide must be audible as intermediate pitches: {analysis['glides'][name]}"
        )
    assert analysis["glides"]["down"]["span_hz"] is not None and analysis["glides"]["down"]["span_hz"] > 5.0, (
        f"the forced glide back down did not move: {analysis['glides']['down']}"
    )
    assert analysis["bands_n3"]["sub_below_120"] > 0.35, (
        f"the bass must be sub-dominant at the note: {analysis['bands_n3']}"
    )
    assert analysis["bands_n3"]["high_above_2000"] < 0.15, (
        f"the top end is not controlled (fizz): {analysis['bands_n3']}"
    )
    h2 = analysis["harmonics_n3"]["h2_db"]
    assert h2 is not None and -24.0 < h2 < 6.0, f"the upper harmonic must exist and stay controlled: {h2}"
    assert analysis["attack"]["rise_ms"] is not None and analysis["attack"]["rise_ms"] < 40.0, analysis["attack"]
    assert analysis["tail"]["ratio_vs_n3"] < 0.10, f"the tail rings: {analysis['tail']}"
