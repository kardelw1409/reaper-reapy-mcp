"""P19 probe K — is the spectral morph *inert*, or was that a claim about one point?

**Why.** The P19 journal said the morph is "measured inert on the single-frame Init
table". An independent reviewer (GLM-5.3 Flash, 2026-10-11) re-analysed the morph
probe's own WAVs and reported: `Low Pass @ 100` is bit-identical to `None @ 0`, but
the *baseline* arm - `Low Pass @ 60` - differs from both by up to 0.0072 in 320,344
samples, and `Shepard Tone @ 100` differs too. So the render *does* respond to the
morph state, and my root cause ("a single-frame table makes it inert") was not
isolated: "amount 100 is the no-op corner of the low-pass morph" fits the same data.

**Decision rule, stated before measuring.** One control at a time, everything else
frozen at the accepted 808 patch: `None @ 0`, `Low Pass @ 25`, `Low Pass @ 60`,
`Low Pass @ 100`. Same phrase window, same render settings, one render per arm.
Two consecutive renders of the *same* state are bit-identical on this instance
(measured in the P19 morph probe), so a non-zero sample diff between arms is a
state effect. The verdict is computed from the measured diffs - never from a
missing one - and the *journal* is corrected to whatever the measurement says,
including if that means a root cause of mine was wrong.

Ends by writing `Low Pass... ` back to the accepted state: morph `None` @ 0.
"""

from __future__ import annotations

import json
import os
from typing import Any

import numpy as np
import pytest

import live_harness as H
import probe_p19_common as C

pytestmark = pytest.mark.live_write

ARMS = (
    ("none_0", {"osc_1_spectral_morph_type": "None", "osc_1_spectral_morph_amount": 0.0}),
    ("lowpass_25", {"osc_1_spectral_morph_type": "Low Pass", "osc_1_spectral_morph_amount": 25.0}),
    ("lowpass_60", {"osc_1_spectral_morph_type": "Low Pass", "osc_1_spectral_morph_amount": 60.0}),
    ("lowpass_100", {"osc_1_spectral_morph_type": "Low Pass", "osc_1_spectral_morph_amount": 100.0}),
)
ACCEPTED = ARMS[0][1]


def read_mono(path):
    import soundfile as sf

    audio, fs = sf.read(str(path), dtype="float64", always_2d=True)
    return audio.mean(axis=1), fs


def test_probe_k_is_the_morph_inert_or_just_cornered():
    try:
        import reapy

        project = reapy.Project()
        project.id  # noqa: B018 - reachability probe
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the probe did not run: {exc}")

    os.environ.setdefault("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    from reaper_reapy_mcp.server import create_server

    server = create_server(debug=False)
    track_index = C.track_by_name(server)
    window_start, _item = C.paragraph_window(server, track_index)
    instance = {"track_index": track_index, "fx_index": 0}

    report: dict[str, Any] = {
        "probe": "p19_morph_amounts",
        "utc": H.utc_now(),
        "live_write": True,
        "note": "one control at a time; every other control is frozen at the accepted 808 patch",
        "arms": {},
        "diffs": {},
        "verdict": {},
    }

    def write(controls: dict[str, Any], label: str) -> dict[str, Any]:
        payload = C.call(server, "plugin_write", {"instance": instance, "request": {"controls": controls}, "dry_run": False})
        data = payload.get("data") or {}
        assert payload.get("ok") is True, (label, json.dumps(payload, default=str)[:600])
        assert data.get("committed") is True, (label, data)
        return {"committed": True, "verdict": ((data.get("outcome") or {}).get("verdict"))}

    try:
        for label, controls in ARMS:
            entry: dict[str, Any] = {"controls": controls, "write": write(controls, label)}
            out = H.EVIDENCE_DIR / f"p19_morph_amounts_{label}__{H.run_stamp()}.wav"
            if out.exists():
                out = out.with_name(out.name.replace(".wav", f"_{len(report['arms']):02d}.wav"))
            entry.update(C.render_window(server, project, out, window_start))
            entry["file"] = str(out)
            entry["analysis"] = C.analyse(out, with_pitch=False)
            report["arms"][label] = entry
    finally:
        write(ACCEPTED, "restore_to_accepted(None@0)")
        # pairwise diffs, from the rendered audio (never from a summary)
        labels = list(report["arms"])
        mono: dict[str, np.ndarray] = {}
        for label in labels:
            try:
                mono[label], _fs = read_mono(report["arms"][label]["file"])
            except Exception as exc:  # pragma: no cover - a missing render is reported
                report["diffs"][f"{label}:read"] = f"failed: {exc}"
        for i, left in enumerate(labels):
            for right in labels[i + 1 :]:
                if left not in mono or right not in mono:
                    continue
                n = min(len(mono[left]), len(mono[right]))
                diff = np.abs(mono[left][:n] - mono[right][:n])
                report["diffs"][f"{left}_vs_{right}"] = {
                    "max_abs": float(diff.max()) if n else None,
                    "n_nonzero": int(np.count_nonzero(diff)),
                    "n_samples": int(n),
                    "bit_identical": bool(n and not np.any(diff)),
                }
        none_vs = {key: value for key, value in report["diffs"].items() if "none_0_vs_lowpass" in key}
        report["verdict"] = {
            "low_pass_amounts_that_change_the_render": [
                key.split("none_0_vs_")[-1]
                for key, value in none_vs.items()
                if value.get("max_abs") is not None and value["max_abs"] > 0.0
            ],
            "low_pass_amounts_bit_identical_to_none": [
                key.split("none_0_vs_")[-1]
                for key, value in none_vs.items()
                if value.get("bit_identical") is True
            ],
            "reading": (
                "if only amount 100 is identical to None@0, the earlier claim that the morph is "
                "inert (attributed to the single-frame table) is refuted: the amount mapping's "
                "corner is what the probe had measured, and the morph does act at other amounts"
            ),
        }
        path = H.write_run_json("p19_morph_amounts", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        for label, entry in report["arms"].items():
            a = entry["analysis"]
            print(
                f"  {label:12s} h2={a['h2_db']!s:>7} h3={a['h3_db']!s:>7} centroid={a['centroid_hz']!s:>7} "
                f"sub={a['sub_below_120']} peak={a['peak_dbfs']} file={entry['file'].split(chr(92))[-1]}"
            )
        for key, value in report["diffs"].items():
            print(f"  diff {key}: max {value.get('max_abs')} nonzero {value.get('n_nonzero')} identical={value.get('bit_identical')}")
        print(f"verdict: {json.dumps(report['verdict'])}")

    assert report["arms"], "no arm rendered"
    assert all("bit_identical" in value for value in report["diffs"].values() if "n_samples" in value)
    assert report["verdict"]["low_pass_amounts_bit_identical_to_none"] is not None
    # the accepted state must be what the probe leaves behind
    morph = C.call(server, "plugin_read", {"instance": instance, "controls": ["osc_1_spectral_morph_type", "osc_1_spectral_morph_amount"], "include_display": True})["data"]["values"]
    assert morph["osc_1_spectral_morph_type"]["display"] == "None", morph
    assert float(morph["osc_1_spectral_morph_amount"]["native"]) == pytest.approx(0.0, abs=1e-6), morph
