"""P18 acceptance: three role tracks, three distinct basses, one batch write each.

The practical-acceptance brief's core scenario, end to end, through the registered
tools only:

1. **instance discovery** - `plugin_instances` proves each role track hosts its own
   Vital and the three locations are distinct;
2. **batch-first write** - each patch (50 controls: sources, character, envelope,
   glide, global, level) goes through **one** `plugin_write` dry run and **one**
   applied call, with the response's own accounting recorded (host reads, write
   calls, per-control read-back rows);
3. **MIDI** - each role track carries one 4 s phrase with overlapping notes for the
   legato slides and a gapped note for the forced glide; the item is created only
   if no phrase item is present, so a re-run never stacks duplicates;
4. **renders** - one WAV per track through the registered classic `render_project`
   (render settings snapshotted and restored around the call), saved into the
   evidence directory;
5. **measurements** - pitch trajectory (autocorrelation), tuning error per note,
   glide transit frames, band balance, harmonic ratios, attack rise, tail - plus a
   cross-instance isolation check (distinct written values read back per track);
6. **isolation proof** - one marker control per instance (`filter_1_cutoff`:
   50 / 86 / 96) read from all three locations at the end: each instance must
   report its own value, so profile or value caches cannot have crossed targets.

The phrases stay on the three role tracks (the brief asks for them as material);
nothing else in the project is touched, and the project is never saved.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

import live_harness as H

pytestmark = pytest.mark.live_write

PATCHES = json.loads((Path(__file__).resolve().parent / "p18_bass_patches.json").read_text(encoding="utf-8"))
TRACKS = {"A": 1, "B": 2, "C": 3}
MARKERS = {"A": 50.0, "B": 86.0, "C": 96.0}  # filter_1_cutoff: the per-track written value
ITEM_LENGTH = 4.0
PHRASE = [
    {"pitch": 28, "start": 0.2, "length": 0.9, "velocity": 96},  # E1 41.20 Hz
    {"pitch": 31, "start": 0.9, "length": 0.9, "velocity": 96},  # G1 49.00 Hz, overlaps n1
    {"pitch": 33, "start": 1.6, "length": 0.8, "velocity": 96},  # A1 55.00 Hz, overlaps n2
    {"pitch": 28, "start": 2.6, "length": 1.0, "velocity": 96},  # back down (forced glide)
]
NOTES_HZ = {28: 41.2034446, 31: 48.9994295, 33: 54.9999999}
STEADY = {"n1": (0.30, 0.80), "n2": (1.30, 1.55), "n3": (1.90, 2.30)}
GLIDES = {"up_1": (0.92, 1.10), "up_2": (1.62, 1.80), "down": (2.62, 2.90)}
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
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


def _must(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    payload = call(mcp, name, arguments)
    status = payload.get("status")
    if status not in (None, "success") or payload.get("ok") is False:
        raise AssertionError(f"{name} failed: {json.dumps(payload, default=str)[:600]}")
    return payload


# ---------------------------------------------------------------------------
# audio analysis (the same maths the P13 probe uses)
# ---------------------------------------------------------------------------


def _autocorr_f0(segment: np.ndarray, fs: int, fmin: float = 25.0, fmax: float = 200.0) -> float | None:
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
            {"t": start / fs, "rms": float(np.sqrt(np.mean(seg**2))), "f0": _autocorr_f0(seg, fs)}
        )
    return track


def _rms(mono: np.ndarray, fs: int, lo: float, hi: float) -> float:
    seg = mono[int(lo * fs) : int(hi * fs)]
    return float(np.sqrt(np.mean(seg**2))) if len(seg) else 0.0


def analyse(path: Path) -> dict[str, Any]:
    audio, fs = sf.read(str(path), dtype="float64", always_2d=True)
    mono = audio.mean(axis=1)
    track = _pitch_track(mono, fs)
    peak = float(np.max(np.abs(mono)))
    floor = 0.05 * max((row["rms"] for row in track), default=0.0)

    seg = mono[int(STEADY["n3"][0] * fs) : int(STEADY["n3"][1] * fs)]
    windowed = seg * np.hanning(len(seg))
    power = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    total = float(power.sum()) or 1e-12
    spectrum = np.abs(np.fft.rfft(windowed))

    def mag_at(f: float) -> float:
        band = (freqs >= f * 0.985) & (freqs <= f * 1.015)
        return float(spectrum[band].max()) if band.any() else 0.0

    f0 = NOTES_HZ[33]
    m1, m2, m3 = mag_at(f0), mag_at(2 * f0), mag_at(3 * f0)

    def median_pitch(lo: float, hi: float) -> float | None:
        values = [r["f0"] for r in track if r["f0"] and r["rms"] >= floor and lo <= r["t"] <= hi]
        return float(np.median(values)) if values else None

    steady = {name: median_pitch(lo, hi) for name, (lo, hi) in STEADY.items()}
    steady_err = {
        name: (None if steady[name] is None else round(12 * np.log2(steady[name] / NOTES_HZ[[28, 31, 33][i]]), 2))
        for i, name in enumerate(STEADY)
    }
    glides = {}
    for name, (lo, hi) in GLIDES.items():
        pitches = [r["f0"] for r in track if r["f0"] and r["rms"] >= floor and lo <= r["t"] <= hi]
        origin, target = (
            (NOTES_HZ[28], NOTES_HZ[31]) if name == "up_1"
            else (NOTES_HZ[31], NOTES_HZ[33]) if name == "up_2"
            else (NOTES_HZ[33], NOTES_HZ[28])
        )
        intermediate = [v for v in pitches if min(origin, target) * 1.05 < v < max(origin, target) * 0.95]
        glides[name] = {
            "intermediate_frames": len(intermediate),
            "first_hz": None if not pitches else round(pitches[0], 1),
            "last_hz": None if not pitches else round(pitches[-1], 1),
            "span_hz": None if len(pitches) < 2 else round(max(pitches) - min(pitches), 1),
        }

    lo_i, hi_i = int(ATTACK_WINDOW[0] * fs), int(ATTACK_WINDOW[1] * fs)
    step = 64
    envelope = [
        float(np.sqrt(np.mean(mono[i : i + step] ** 2)))
        for i in range(lo_i, min(hi_i, len(mono) - step), step)
    ]
    reference = _rms(mono, fs, *STEADY["n1"])
    t10 = next((i for i, e in enumerate(envelope) if e >= 0.10 * reference), None)
    t90 = next((i for i, e in enumerate(envelope) if e >= 0.90 * reference), None)
    rise_ms = None if t10 is None or t90 is None else round(1000.0 * (t90 - t10) * step / fs, 1)

    tail = _rms(mono, fs, *TAIL)
    return {
        "peak_dbfs": round(20.0 * float(np.log10(max(peak, 1e-12))), 2),
        "rms_dbfs": round(20.0 * float(np.log10(max(_rms(mono, fs, 0.2, 3.8), 1e-12))), 2),
        "n_clipped_samples": int(np.sum(np.abs(mono) >= 0.9999)),
        "sub_below_120": round(float(power[freqs < 120.0].sum()) / total, 3),
        "mid_150_500": round(float(power[(freqs >= 150.0) & (freqs < 500.0)].sum()) / total, 3),
        "high_above_2000": round(float(power[freqs > 2000.0].sum()) / total, 5),
        "centroid_hz": round(float((freqs * power).sum() / total), 1),
        "h2_db": None if m1 <= 0 else round(20.0 * float(np.log10(max(m2, 1e-12) / m1)), 2),
        "h3_db": None if m1 <= 0 else round(20.0 * float(np.log10(max(m3, 1e-12) / m1)), 2),
        "steady_pitch_hz": steady,
        "steady_error_st": steady_err,
        "attack_rise_ms": rise_ms,
        "tail_ratio_vs_n3": round(tail / max(_rms(mono, fs, *STEADY["n3"]), 1e-9), 3),
        "glides": glides,
    }


# ---------------------------------------------------------------------------
# the acceptance flow
# ---------------------------------------------------------------------------


def test_three_basses_are_written_rendered_and_measured():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the acceptance did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)

    report: dict[str, Any] = {
        "probe": "p18_bass_acceptance",
        "utc": H.utc_now(),
        "live_write": False,
        "brief": "three role tracks, one batch write per bass, one render per bass, measured",
        "instances": {},
        "tracks": {},
        "isolation": {},
        "metrics": {"calls": 0, "read_calls": 0, "write_calls": 0, "render_calls": 0},
        "evidence": None,
    }

    def counted(name: str) -> None:
        report["metrics"]["calls"] += 1
        if "read" in name:
            report["metrics"]["read_calls"] += 1
        if "write" in name:
            report["metrics"]["write_calls"] += 1
        if name == "render_project":
            report["metrics"]["render_calls"] += 1

    # ---- 1. discovery: each role track hosts its own Vital ---------------------
    instances = _must(server, "plugin_instances", {})
    counted("plugin_instances")
    located: dict[str, dict[str, Any]] = {}
    for role, track_index in TRACKS.items():
        found = [
            inst for inst in (instances.get("data") or {}).get("instances", [])
            if int((inst.get("location") or {}).get("track_index", -1)) == track_index
        ]
        assert len(found) == 1, f"{role}: expected exactly one Vital on track {track_index}, found {len(found)}"
        located[role] = {"location": dict(found[0]["location"]), "identity": dict(found[0]["identity"])}
        assert found[0]["identity"]["fx_name"] == "VST3i: Vital (Vital Audio)", found[0]["identity"]
    locations = {tuple(row["location"].values()) for row in located.values()}
    assert len(locations) == 3, f"instances must be distinct targets: {locations}"
    report["instances"] = located

    import reapy
    from reapy import reascript_api as RPR

    pid = project.id

    # ---- 2. per track: batch write, phrase item, render, measure ---------------
    for role, track_index in TRACKS.items():
        patch = PATCHES[role]["controls"]
        instance = {"track_index": track_index, "fx_index": 0}

        t0 = time.perf_counter()
        dry = call(server, "plugin_write", {"instance": instance, "request": {"controls": patch}, "dry_run": True})
        counted("plugin_write")
        dry_data = (dry or {}).get("data") or {}
        assert dry.get("ok") is True and dry_data.get("would_write") is True, dry
        dry_s = round(time.perf_counter() - t0, 2)

        t0 = time.perf_counter()
        commit = call(server, "plugin_write", {"instance": instance, "request": {"controls": patch}, "dry_run": False})
        counted("plugin_write")
        commit_data = (commit or {}).get("data") or {}
        outcome = commit_data.get("outcome") or {}
        assert commit.get("ok") is True and commit_data.get("committed") is True, commit
        assert outcome.get("verdict") == "ALL_VERIFIED", outcome
        commit_s = round(time.perf_counter() - t0, 2)
        rows = ((outcome.get("channels") or {}).get("parameters") or {}).get("rows") or []
        assert rows and all(row.get("agrees") for row in rows), "every written control needs an agreeing read-back"

        # the phrase (reuse an existing 4 s item on this track if present)
        items = _must(server, "get_items_in_time_range", {"track_index": track_index, "start_time": 0.0, "end_time": 1200.0})
        phrase_items = [item for item in (items.get("items") or []) if abs(float(item["length"]) - ITEM_LENGTH) < 0.01]
        reused = bool(phrase_items)
        if phrase_items:
            item = phrase_items[0]
            item_id = item.get("direct_item_id") or item.get("track_pos_idx")
            window_start = float(item["position"])
        else:
            others = []
            for other_index in range(int(_must(server, "get_track_count", {})["track_count"])):
                found = _must(server, "get_items_in_time_range", {"track_index": other_index, "start_time": 0.0, "end_time": 1200.0})
                others.extend(float(i["position"]) + float(i["length"]) for i in (found.get("items") or []))
            window_start = round(max(others, default=0.0) + 5.0 + list(TRACKS).index(role) * 5.0, 3)
            created = _must(server, "create_midi_item", {"track_index": track_index, "start_time": window_start, "length_time": ITEM_LENGTH})
            item_id = created.get("direct_item_id")
            notes = [
                {"pitch": n["pitch"], "start_time": round(window_start + n["start"], 4),
                 "length_time": n["length"], "velocity": n["velocity"]}
                for n in PHRASE
            ]
            _must(server, "add_midi_notes", {"track_index": track_index, "item": item_id, "notes": notes})

        # the render, with the classic settings snapshot/restore around it
        int_before = {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in RENDER_INT_KEYS}
        str_before = {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in RENDER_STR_KEYS}
        selection_before = (project.time_selection.start, project.time_selection.end)
        out_path = H.EVIDENCE_DIR / f"p18_bass_{role}__{H.run_stamp()}.wav"
        rendered: dict | None = None
        try:
            rendered = call(
                server,
                "render_project",
                {"output_file": str(out_path), "start_time": window_start, "end_time": window_start + ITEM_LENGTH,
                 "samplerate": 44100, "channels": 2, "kwargs": {}},
            )
            counted("render_project")
        finally:
            for key, value in int_before.items():
                RPR.GetSetProjectInfo(pid, key, value, True)
            for key, value in str_before.items():
                RPR.GetSetProjectInfo_String(pid, key, value, True)
        assert (rendered or {}).get("status") == "success", rendered
        assert out_path.is_file() and out_path.stat().st_size > 100_000, out_path

        report["tracks"][role] = {
            "role": PATCHES[role]["role"],
            "track_index": track_index,
            "n_controls": len(patch),
            "write": {
                "dry_s": dry_s,
                "commit_s": commit_s,
                "dry_verdict": (dry_data.get("outcome") or {}).get("verdict"),
                "commit_verdict": outcome.get("verdict"),
                "host": outcome.get("host"),
                "read_back_rows": len(rows),
                "all_rows_agree": True,
            },
            "phrase": {"window_start": window_start, "item_id": item_id, "reused": reused, "n_notes": len(PHRASE)},
            "render": {
                "file": str(out_path),
                "settings_restored": bool(
                    {key: float(RPR.GetSetProjectInfo(pid, key, 0.0, False)) for key in RENDER_INT_KEYS} == int_before
                    and {key: str(RPR.GetSetProjectInfo_String(pid, key, "", False)[3]) for key in RENDER_STR_KEYS} == str_before
                ),
                "time_selection_restored": (project.time_selection.start, project.time_selection.end) == selection_before,
            },
            "analysis": analyse(out_path),
        }
        assert report["tracks"][role]["render"]["settings_restored"], "render settings must be restored"

    # ---- 3. isolation: marker values per instance, read from all three ---------
    for role, track_index in TRACKS.items():
        read = call(server, "plugin_read", {"instance": {"track_index": track_index, "fx_index": 0}, "controls": ["filter_1_cutoff", "portamento_time"]})
        counted("plugin_read")
        values = (read.get("data") or {}).get("values") or {}
        report["isolation"][role] = {
            "location": {"track_index": track_index, "fx_index": 0},
            "filter_1_cutoff": (values.get("filter_1_cutoff") or {}).get("native"),
            "portamento_time": (values.get("portamento_time") or {}).get("native"),
        }
    for role, expected in MARKERS.items():
        observed = report["isolation"][role]["filter_1_cutoff"]
        assert observed == pytest.approx(expected), (
            f"{role}: filter_1_cutoff reads {observed!r}, expected the value written to that instance ({expected})"
        )
    porta = {role: report["isolation"][role]["portamento_time"] for role in TRACKS}
    assert porta["A"] < porta["B"] < porta["C"], f"the glide times must differ per instance: {porta}"

    # ---- 4. measurements that must hold for every bass -------------------------
    for role, entry in report["tracks"].items():
        analysis = entry["analysis"]
        assert analysis["n_clipped_samples"] == 0, f"{role} clips: {analysis}"
        assert analysis["peak_dbfs"] > -40.0, f"{role} renders silent: {analysis}"
        for note, error in analysis["steady_error_st"].items():
            assert error is not None and abs(error) < 0.25, f"{role} {note} is out of tune: {error} st"
        for glide, row in analysis["glides"].items():
            assert row["span_hz"] is not None and row["span_hz"] > 2.0, f"{role} {glide} does not move: {row}"
        assert analysis["attack_rise_ms"] is not None and analysis["attack_rise_ms"] < 40.0, analysis["attack_rise_ms"]
        assert analysis["tail_ratio_vs_n3"] < 0.10, f"{role} tail rings: {analysis}"
        assert analysis["sub_below_120"] > 0.5, f"{role} is not sub-dominant: {analysis}"

    # distinctness: the three sounds must differ in the measured character
    centroids = {role: report["tracks"][role]["analysis"]["centroid_hz"] for role in TRACKS}
    subs = {role: report["tracks"][role]["analysis"]["sub_below_120"] for role in TRACKS}
    highs = {role: report["tracks"][role]["analysis"]["high_above_2000"] for role in TRACKS}
    assert centroids["A"] < centroids["B"] < centroids["C"], f"timbre ordering broken: {centroids}"
    assert subs["A"] > subs["B"] > subs["C"], f"sub balance ordering broken: {subs}"
    assert highs["C"] > highs["A"] and highs["C"] > highs["B"], f"the dubstep bass must carry the most top: {highs}"

    # ---- 5. the batch-vs-naive ledger -----------------------------------------
    batch_calls = report["metrics"]["calls"]
    n_controls = sum(report["tracks"][role]["n_controls"] for role in TRACKS)
    naive_calls = 3 * n_controls  # per control: read baseline + apply + read-back
    report["metrics"].update(
        {
            "n_controls_total": n_controls,
            "batch_calls": batch_calls,
            "naive_single_control_calls": naive_calls,
            "call_reduction_x": round(naive_calls / max(batch_calls, 1), 1),
            "host_write_calls": sum(
                ((report["tracks"][role]["write"]["host"] or {}).get("write_calls_attempted") or 0) for role in TRACKS
            ),
        }
    )

    path = H.write_run_json("p18_bass_acceptance", report, H.EVIDENCE_DIR)
    report["evidence"] = str(path)
    print(f"\n[p18 acceptance] evidence: {path}")
    for role in TRACKS:
        entry = report["tracks"][role]
        analysis = entry["analysis"]
        print(
            f"[p18 acceptance] {role} {entry['role']}: {entry['n_controls']} controls in one batch, "
            f"centroid {analysis['centroid_hz']} Hz, sub {analysis['sub_below_120']}, "
            f"pitch err {analysis['steady_error_st']}, rises {analysis['attack_rise_ms']} ms"
        )
    print(
        f"[p18 acceptance] calls: {batch_calls} total (naive single-control would be {naive_calls}; "
        f"x{report['metrics']['call_reduction_x']}), host write calls: {report['metrics']['host_write_calls']}"
    )
