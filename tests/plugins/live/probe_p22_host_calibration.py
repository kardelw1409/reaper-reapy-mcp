"""P22 host-calibration probe - **READ ONLY**; writes one artifact + one evidence file.

This is the *preparation* step the capability profile's completeness comes from,
and it is deliberately not part of startup or of the tool path: it walks the
control set once, records what the **host formatter** says, and writes a
versioned artifact whose bytes become a declared source of the profile (so its
hash is cache-key material and a changed artifact invalidates every entry).

What it measures, all without touching a single parameter:

1. ``TrackFX_FormatParamValueNormalized`` at a fixed normalized grid for every
   indexed control - the host's own display text for a value *the caller passes*
   (the documented read-only formatting call);
2. ``TrackFX_GetParameterStepSizes`` - step/small/large/is-toggle per control;
3. ``TrackFX_GetParamEx`` - the host's technical min/max/mid per control;
4. ``TrackFX_GetNamedConfigParm 'param.N.default_value'`` - probed and reported
   even though REAPER 7.53 answers 0 for every VST3 parameter (the *absence* is
   the finding: a default cannot be read, and must not be invented);
5. read-only proof: 31 watched normalized values plus the state-chunk SHA-256
   before/after the whole sweep, and the instance identity.

Run: ``py -3.11 tests/plugins/live/probe_p22_host_calibration.py``
The artifact lands at the provider's default calibration path
(``%LOCALAPPDATA%/reaper_reapy_mcp/calibration/vital_host_calibration.json``), so
the next profile build picks it up; pass ``--out PATH`` to write elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE.parent
for _path in (str(SRC), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from reaper_reapy_mcp.plugins.providers.vital import calibration as CAL  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import depth as D  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import profile_build as PB  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital.provider import VitalProvider  # noqa: E402

EVIDENCE_DIR = HERE / "evidence"
GRID_DIVISIONS = 16  # 17 points: the step that lands on 1/16 switch points exactly


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _profile_identity(profile: dict) -> dict:
    identity = profile["identity"]
    return {
        "fx_name": identity.get("fx_name"),
        "class_id": identity.get("class_id"),
        "module": identity.get("module"),
        "param_count": identity.get("fingerprint", {}).get("param_count"),
    }


def _control_view(profile: dict) -> tuple[list[tuple[str, int, dict]], dict]:
    controls = [
        (str(c["id"]), int(c["binding"]["index"]), c)
        for c in profile["controls"]
        if isinstance((c.get("binding") or {}).get("index"), int)
    ]
    return controls, {str(c["id"]): c for c in profile["controls"]}


def _coverage(profile: dict) -> dict:
    controls = profile["controls"]
    laws: dict[str, int] = {}
    for control in controls:
        kind = str((control.get("law") or {}).get("kind"))
        laws[kind] = laws.get(kind, 0) + 1
    return {
        "controls": len(controls),
        "laws": dict(sorted(laws.items())),
        "range_none": sum(1 for c in controls if c.get("range") is None),
        "unit_none": sum(1 for c in controls if c.get("unit") is None),
        "weak": sum(1 for c in controls if (c.get("evidence") or {}).get("strength") == "weak"),
        "candidates": sum(1 for c in controls if c.get("law_candidate")),
        "confirmed": sum(
            1
            for c in controls
            if ((c.get("evidence") or {}).get("confirmation") or {}).get("state") == "confirmed"
        ),
        "confirmations_declined": sum(
            1
            for c in controls
            if ((c.get("evidence") or {}).get("confirmation") or {}).get("state") == "declined"
        ),
        "units_filled": sum(1 for c in controls if c.get("unit_source")),
        "has_steps": sum(1 for c in controls if c.get("host_steps")),
        "calibration": dict(profile.get("calibration") or {}).get("counts") or {},
        "calibration_state": dict(profile.get("calibration") or {}).get("state"),
    }


def capture(profile: dict, *, out_path: Path) -> tuple[dict, dict]:
    """Walk the grid read-only and return ``(artifact, timings)``."""
    import reapy  # noqa: PLC0415 - only the live probe imports the host client
    from reapy import reascript_api as R  # noqa: PLC0415

    controls, by_id = _control_view(profile)
    grid = [index / GRID_DIVISIONS for index in range(GRID_DIVISIONS + 1)]
    timings: dict[str, float] = {}
    artifact: dict = {
        "kind": CAL.CALIBRATION_KIND,
        "method_version": CAL.CALIBRATION_METHOD_VERSION,
        "generated_utc": _utc(),
        "host": {"name": "reaper", "transport": "reapy"},
        "identity": _profile_identity(profile),
        "grid": grid,
        "controls": {},
    }

    reapy.connect()
    with reapy.inside_reaper():
        track = R.GetTrack(0, 0)
        names = list(R.TrackFX_GetFXName(track, 0, "", 512))
        fx_name = next((n for n in names if isinstance(n, str) and n.startswith("VST")), "")
        artifact["host"]["fx_name_reported"] = fx_name
        artifact["host"]["param_count_reported"] = int(R.TrackFX_GetNumParams(track, 0))

        def fmt(index: int, normalized: float) -> str:
            raw = list(R.TrackFX_FormatParamValueNormalized(track, 0, index, float(normalized), "", 512))
            return str(raw[5]) if len(raw) > 5 else ""

        watch = list(range(0, 775, 25))
        before_values = [float(R.TrackFX_GetParamNormalized(track, 0, i)) for i in watch]
        before_chunk = hashlib.sha256(
            str(R.GetTrackStateChunk(track, "", 20_000_000, False)).encode("utf-8")
        ).hexdigest()

        started = time.perf_counter()
        for control_id, index, control in controls:
            samples = [fmt(index, point) for point in grid]
            numeric_values = [D.display_number(sample) for sample in samples]
            numeric = all(value is not None for value in numeric_values) and all(
                str(sample).strip() for sample in samples
            )
            tokens = {D.display_unit(sample) for sample in samples}
            tokens.discard("")
            unit_token = next(iter(tokens)) if len(tokens) == 1 else None
            descending = bool(
                numeric
                and numeric_values[0] is not None
                and numeric_values[-1] is not None
                and float(numeric_values[0]) > float(numeric_values[-1])
            )
            labels: list[str] = []
            if not numeric:
                for sample in samples:
                    label = str(sample).strip()
                    if label and label not in labels:
                        labels.append(label)
            artifact["controls"][control_id] = {
                "index": index,
                "samples": samples,
                "numeric": bool(numeric),
                "unit_token": unit_token,
                "descending": descending,
                "first": samples[0],
                "last": samples[-1],
                "labels": labels or None,
            }
        timings["display_grid_seconds"] = round(time.perf_counter() - started, 3)

        started = time.perf_counter()
        reported = 0
        for control_id, index, _control in controls:
            raw = list(R.TrackFX_GetParameterStepSizes(track, 0, index, 0.0, 0.0, 0.0, False))
            entry = artifact["controls"][control_id]
            entry["steps"] = {
                "step": float(raw[4]),
                "small": float(raw[5]),
                "large": float(raw[6]),
                "is_toggle": bool(raw[7]),
                "reported": bool(raw[0]),
            }
            reported += 1 if raw[0] else 0
        timings["step_sizes_seconds"] = round(time.perf_counter() - started, 3)
        artifact["host_step_sizes"] = {"reported": reported, "probed": len(controls)}

        started = time.perf_counter()
        uniform = 0
        for _control_id, index, _control in controls:
            raw = list(R.TrackFX_GetParamEx(track, 0, index, 0.0, 0.0, 0.0))
            if float(raw[4]) == 0.0 and float(raw[5]) == 1.0 and float(raw[6]) == 0.5:
                uniform += 1
        timings["param_ex_seconds"] = round(time.perf_counter() - started, 3)
        artifact["host_param_space"] = {
            "call": "TrackFX_GetParamEx",
            "min": 0.0,
            "max": 1.0,
            "mid": 0.5,
            "uniform_0_1": uniform == len(controls),
            "probed": len(controls),
            "note": (
                "every probed parameter reports the normalized 0..1 space: the host exposes no "
                "semantic min/max for this plugin, so a semantic range can only come from the "
                "measured sweep or from provider data"
            ),
        }

        started = time.perf_counter()
        answered = 0
        for _control_id, index, _control in controls:
            raw = list(R.TrackFX_GetNamedConfigParm(track, 0, f"param.{index}.default_value", "", 256))
            if raw[0]:
                answered += 1
        timings["default_value_seconds"] = round(time.perf_counter() - started, 3)
        artifact["host_default_value"] = {
            "call": "TrackFX_GetNamedConfigParm 'param.N.default_value'",
            "answered": answered,
            "probed": len(controls),
            "available": answered > 0,
            "evidence": (
                f"answered for {answered} of {len(controls)} probed controls on REAPER 7.53 / this "
                "VST3 build: the host exposes no parameter default here, so a default can only "
                "come from provider documentation or an active calibration - never from a guess"
            ),
        }

        after_values = [float(R.TrackFX_GetParamNormalized(track, 0, i)) for i in watch]
        after_chunk = hashlib.sha256(
            str(R.GetTrackStateChunk(track, "", 20_000_000, False)).encode("utf-8")
        ).hexdigest()

    artifact["read_only_proof"] = {
        "watched_points": len(watch),
        "values_unchanged": before_values == after_values,
        "chunk_sha256_unchanged": before_chunk == after_chunk,
        "statement": (
            "TrackFX_FormatParamValueNormalized, TrackFX_GetParameterStepSizes, "
            "TrackFX_GetParamEx and TrackFX_GetNamedConfigParm were called; the watched "
            "normalized values and the state chunk are byte-identical before and after"
        ),
    }
    if not artifact["read_only_proof"]["values_unchanged"] or not artifact["read_only_proof"][
        "chunk_sha256_unchanged"
    ]:
        raise SystemExit("read-only proof FAILED - something wrote to the FX; artifact not written")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(artifact, indent=1, sort_keys=True, ensure_ascii=False)
    # ``newline="\n"`` matters: the plain form translates LF to CRLF on Windows, so
    # the file's bytes would differ from the bytes a digest is taken over - and this
    # file's bytes are cache-key material (a declared profile source), so the evidence
    # must record the digest of what is actually on disk.
    out_path.write_text(text, encoding="utf-8", newline="\n")
    return artifact, timings


def main(argv: list[str]) -> int:
    out_path = Path(argv[argv.index("--out") + 1]) if "--out" in argv else Path(
        CAL.default_calibration_path()
    )

    started = time.perf_counter()
    # the baseline must be *calibration-free*: a previous run's artifact must not
    # silently become the "before" measurement. "No artifact at all" is expressed as
    # the provider reporting **no artifact path**, not as a path that fails to read
    # - the latter is a *refusal* state, and reporting a deliberate baseline as a
    # broken artifact would misstate what the "before" numbers mean.
    baseline_provider = VitalProvider()
    baseline_provider.calibration_artifact_path = lambda: None  # type: ignore[assignment]
    baseline_profile = baseline_provider.build_profile(
        {
            "fx_name": PB.VITAL_FX_NAME,
            "class_id": PB.VITAL_CLASS_ID,
            "module": PB.VITAL_MODULE,
            "param_count": 2858,
        }
    )
    baseline = _coverage(baseline_profile)

    artifact, timings = capture(baseline_profile, out_path=out_path)
    artifact_bytes = out_path.read_bytes()
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()

    calibrated_provider = VitalProvider(calibration_path=str(out_path))
    after_profile = calibrated_provider.build_profile(
        {
            "fx_name": PB.VITAL_FX_NAME,
            "class_id": PB.VITAL_CLASS_ID,
            "module": PB.VITAL_MODULE,
            "param_count": 2858,
        }
    )
    after = _coverage(after_profile)

    evidence = {
        "probe": "p22_host_calibration",
        "utc": _utc(),
        "read_only": True,
        "artifact": {
            "path": str(out_path),
            "sha256": artifact_sha,
            "bytes": len(artifact_bytes),
            "sha256_matches_disk": hashlib.sha256(out_path.read_bytes()).hexdigest() == artifact_sha,
        },
        "timings": timings,
        "identity": artifact["identity"],
        "read_only_proof": artifact["read_only_proof"],
        "host_step_sizes": artifact["host_step_sizes"],
        "host_param_space": artifact["host_param_space"],
        "host_default_value": artifact["host_default_value"],
        "coverage_before": baseline,
        "coverage_after": after,
        "delta": {
            "units_filled": after["units_filled"] - baseline["units_filled"],
            "unit_none": baseline["unit_none"] - after["unit_none"],
            "confirmed_evidence": after["confirmed"],
            "confirmations_declined": after["confirmations_declined"],
            "steps_recorded": after["has_steps"],
            "weak_before": baseline["weak"],
            "weak_after": after["weak"],
            "candidates": after["candidates"],
        },
        "seconds": round(time.perf_counter() - started, 3),
    }
    text = json.dumps(evidence, indent=2, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = EVIDENCE_DIR / f"p22_host_calibration__{_run_stamp()}__{digest}.json"
    if evidence_path.exists():
        evidence_path = EVIDENCE_DIR / f"p22_host_calibration__{_run_stamp()}__{digest}__2.json"
    evidence_path.write_text(text, encoding="utf-8")

    print(f"artifact: {out_path}  sha256={artifact_sha[:16]}  {evidence['artifact']['bytes']:,} bytes")
    print(f"evidence: {evidence_path}")
    print(
        "timings:",
        timings,
        f" total {evidence['seconds']} s",
    )
    print("read-only proof:", artifact["read_only_proof"]["values_unchanged"], artifact["read_only_proof"]["chunk_sha256_unchanged"])
    print("before:", json.dumps(baseline, sort_keys=True))
    print("after: ", json.dumps(after, sort_keys=True))
    print("delta: ", json.dumps(evidence["delta"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
