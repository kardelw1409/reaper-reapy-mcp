"""P13 live acceptance: an 808 glide bass, built through the registered V3 tools.

Owner's brief (2026-10-11): the working criterion is a real musical result - a
deep, confident **808 glide bass** for dubstep/rap living in the current Vital
instance, produced by **compact batched calls** through the registered Universal
Plugin MCP V3 tools, then proven by measurement. The old adapter is a reference
for semantics, never the execution path.

What this probe does, in order:

1. resolves the instance and captures a **complete** recovery set (an incomplete
   one is a refusal, not a smaller test) - this is also the *manual restore*
   record for the patch it is about to create;
2. reads the candidate controls through the registered `plugin_read` and proves
   each declared index carries the declared control on the host right now;
3. computes the patch's expected values twice: once through the layer's own
   declared laws, and once through the **registry's measured sweep model** for
   the controls whose display axis is not linear (envelope times, portamento);
4. runs `plugin_write(dry_run=true)` and requires `would_write: true` with **zero**
   write verbs at the host boundary;
5. applies the batch with **one** `plugin_write(dry_run=false)`, addressed by
   location (a token-carrying mutation is refused by design);
6. verifies by an independent `plugin_read`, the full parameter-vector diff
   (exactly the written controls may move), the located block digest, the
   outside-span check and the document-leaf diff (with the mismatching rows kept,
   because the P12 run could not name the leaf it measured);
7. writes the evidence artifact **before** the assertions, so a late probe defect
   cannot lose the measurement.

The sound **stays**: the deliverable is the patch in the live instance. The
transaction's own recovery set is captured in memory by the engine; the artifact
carries the captured display values per written control, which is what a manual
restore (or a later round) needs.

Measured model used for the nonlinear controls (provenance: the registry's
257-point sweeps, `vital_scales_full.json`, and the live read of the same
controls):

- envelope times: ``seconds = native ** 4`` with ``native = norm * range.max``
  (the sweep's max display ``32.0003 secs`` equals ``2.378419804394196 ** 4``);
- portamento time: ``seconds = 2 ** (-10 + 14 * norm)`` (the sweep runs
  ``0.000976562 s`` .. ``16 s`` = ``2 ** -10`` .. ``2 ** 4``).

Both models are cross-checked against the live read-back in step 6, so a wrong
model shows up as a *measured* disagreement rather than a silent assumption.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest

import live_harness as H

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import binding as B
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins import values as V
from reaper_reapy_mcp.plugins.errors import NotConnected, PluginError
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.model import StateRevision, text_digest
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider

pytestmark = pytest.mark.live_write

#: The contract's tolerance for a written normalized parameter
#: (`profile.verification.tolerances["parameter.normalized"]`).
TOLERANCE_NORMALIZED = 1e-3

#: Glide the patch is built around: 90 ms of portamento. The display value sent is
#: derived from the live range and the measured model below - never hard-coded.
PORTAMENTO_TARGET_SECONDS = 0.09
PORTAMENTO_MODEL = {
    "seconds_min": 0.000976562,
    "seconds_max": 16.0,
    "native_min": -10.0,
    "native_span": 14.0,
    "source": (
        "vital_scales_full.json scales['Portamento Time'] (257-state sweep): "
        "norm 0 -> 0.000976562 s = 2**-10, norm 1 -> 16 s = 2**4"
    ),
}

#: ``seconds = native ** 4``, ``native = norm * range.max``.
ENV_TIME_POWER = 4.0
ENV_TIME_MODEL = {
    "source": (
        "vital_scales_full.json scales['Envelope 1 Attack'] (257-state sweep): "
        "max display '32.0003 secs' = 2.378419804394196 ** 4; live range.max equals it"
    ),
}

#: The 808 patch. Every value is in the layer's **display space** (the space
#: `plugin_read` reports); enum members are the profile's own measured labels.
#: `distortion_type` is deliberately absent: the live instance already reads
#: `Soft Clip`, so writing it would be a no-change batch item.
REQUEST_CONTROLS: dict[str, Any] = {
    "filter_1_on": "On",          # the sub must pass through the lowpass at all
    "filter_1_cutoff": 80.0,      # ~831 Hz, 12 dB: keeps 2nd/3rd harmonic, kills fizz
    "filter_1_resonance": 0.2,    # low: no ringing
    "distortion_on": "On",        # the saturation stage
    "distortion_drive": 10.0,     # dB: dense but controlled
    "distortion_mix": 0.45,       # parallel blend: controllable harmonic content
    "env_1_attack": 0.25,         # native**4 = ~3.9 ms: a clean, definite attack
    "env_1_release": 0.62,        # native**4 = ~148 ms: no long boom
    "polyphony": 1.0,             # monophonic execution - glide requires it
    "legato": "On",               # overlapping notes glide without retrigger
    "portamento_force": "On",     # glide between successive notes, not only overlaps
    "stereo_routing": 0.0,        # mono/centred low end
}

#: Why each control is in the patch (recorded per row in the artifact).
WHY: dict[str, str] = {
    "filter_1_on": "without the filter stage the saturation harmonics reach the output unfiltered",
    "filter_1_cutoff": "lowpass above the sub's harmonics: audible on small speakers, no fizz",
    "filter_1_resonance": "no resonant ring on a bass; the character comes from saturation",
    "distortion_on": "the controlled upper-harmonic stage",
    "distortion_drive": "dense but not runaway saturation",
    "distortion_mix": "parallel: the clean sub stays under the saturated copy",
    "env_1_attack": "definite attack without a click",
    "env_1_release": "controlled tail; no excessive boom",
    "polyphony": "monophonic execution: the precondition of any glide",
    "legato": "slides do not retrigger the envelope - the 808 slide feel",
    "portamento_force": "slides happen between successive notes, not only full overlaps",
    "stereo_routing": "bass stays centred/mono",
    "portamento_time": "the glide speed itself (derived from the measured model)",
}


def call(mcp: Any, name: str, arguments: dict | None = None) -> dict:
    """Call a registered tool and read its payload, whatever shape it answers.

    FastMCP answers a **blocks list** for the plugin tools and a
    ``(blocks, structured)`` tuple for the classic tools (measured in the audio
    probe); assuming one shape fails on the other.
    """
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((block.text for block in blocks if getattr(block, "text", None)), None)
    if text is not None:
        return json.loads(text)
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    raise RuntimeError(f"the tool {name!r} answered no readable payload")


class VerbCounter:
    """Counts the bridge's two write verbs; every call is delegated unchanged."""

    VERBS = ("write_params", "write_state_slice")

    def __init__(self, bridge: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        for verb in self.VERBS:
            original = getattr(bridge, verb)

            def wrapper(*args: Any, _verb: str = verb, _fn: Any = original, **kwargs: Any) -> Any:
                self.calls.append({"verb": _verb, "kwargs": sorted(kwargs)})
                return _fn(*args, **kwargs)

            setattr(bridge, verb, wrapper)

    def count(self, verb: str | None = None) -> int:
        return len([row for row in self.calls if verb is None or row["verb"] == verb])

    def mark(self) -> int:
        return len(self.calls)

    def since(self, mark: int, verb: str | None = None) -> list[dict[str, Any]]:
        return [row for row in self.calls[mark:] if verb is None or row["verb"] == verb]


def _live_vectors(bridge: Any, target: Any) -> tuple[dict[str, float], Any, dict[str, Any]]:
    """The full parameter vector, the located slice and the decoded document now."""
    chunk = bridge.read_state_chunk(target.ref)
    facts = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    slice_ = bridge.locate_state_slice(target.ref, facts, chunk)
    document = target.codec.decode(slice_)
    count = int(facts.param_count or 0)
    idents = bridge.param_idents(target.ref, list(range(count)))
    values = bridge.read_params(target.ref, list(range(count)))
    vector = {str(i): float(v) for i, v in zip(idents, values) if i}
    return vector, slice_, document


def _portamento_display(range_min: float, range_max: float) -> dict[str, Any]:
    """The display value that lands the host on the target seconds.

    Two relations are composed, and both are *measured*:

    - the host's own axis: ``seconds = 2 ** (-10 + 14 * norm)`` (the sweep);
    - the layer's declared law: ``norm = (display - range.min) / (range.max - range.min)``.

    Writing ``display`` therefore lands at the target time iff
    ``display = norm_target * (range.max - range.min) + range.min``.
    """
    seconds_min = PORTAMENTO_MODEL["seconds_min"]
    seconds_max = PORTAMENTO_MODEL["seconds_max"]
    native_min = PORTAMENTO_MODEL["native_min"]
    native_span = PORTAMENTO_MODEL["native_span"]
    norm_target = (math.log(PORTAMENTO_TARGET_SECONDS / seconds_min) / math.log(2.0)) / native_span
    display = range_min + norm_target * (range_max - range_min)
    return {
        "target_seconds": PORTAMENTO_TARGET_SECONDS,
        "norm_target": norm_target,
        "display": display,
        "model": dict(PORTAMENTO_MODEL),
        "model_check_seconds_min": seconds_min,
        "model_check_seconds_max": seconds_max,
        "model_native_min": native_min,
    }


def test_the_808_glide_bass_is_built_by_one_verified_batch():
    bridge = R.ReaperHostBridge(timeout=60.0)
    try:
        bridge.list_tracks()
    except NotConnected as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"REAPER is not reachable, so the acceptance did not run: {exc.message}")
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the host answered with an unreadable contract: {exc.code}: {exc.message}")

    provider = VitalProvider()
    try:
        target = H.resolve_target(bridge, provider)
    except PluginError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the harness refused to pick a target: {exc.code}: {exc.message}")

    baseline = H.capture_baseline(bridge, target)
    assert baseline.complete, f"the recovery set is incomplete: {baseline.not_captured}"

    counter = VerbCounter(bridge)
    config = A.PluginConfig(provider_ids=("vital",), bridge_timeout=60.0, env={})
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("p13-bass")
    T.setup_plugin_tools(server, config=config)
    A._BRIDGE = bridge  # the bridge this probe validated (the accepted pattern)
    A._REGISTRY = None

    report: dict[str, Any] = {
        "probe": "p13_bass_acceptance",
        "utc": H.utc_now(),
        "live_write": True,
        "brief": "one verified batch builds an 808 glide bass; the sound stays",
        "target": {
            "fx_name": target.facts.fx_name,
            "location": dict(target.ref.location),
            "identity": provider.identify(target.facts),
            "param_count": target.facts.param_count,
            "session_handle": target.facts.session_handle,
        },
        "calls": [],
        "profile": {},
        "request": {},
        "expected": {},
        "controls_before": {},
        "baseline": {
            "chunk_digest": baseline.chunk_digest,
            "block_digest": baseline.block_digest,
            "revision": baseline.revision.digest,
            "parameters": len(baseline.parameters),
            "settings_leaves": len(H._settings_leaves(baseline.document)),
            "derived_leaves": list(baseline.derived_leaves),
            "not_captured": list(baseline.not_captured),
        },
        "steps": [],
        "dry_run": None,
        "commit": None,
        "after": None,
        "blocker": None,
        "refusals": [],
        "evidence": None,
    }

    def step(name: str, envelope: dict, *, extra: dict | None = None) -> dict:
        outcome = envelope.get("outcome") or {}
        record = {
            "step": name,
            "ok": bool(envelope.get("ok")),
            "error": (envelope.get("error") or {}).get("code"),
            "verdict": outcome.get("verdict"),
            "final_state": outcome.get("final_state"),
            "host": outcome.get("host"),
            "meta_keys": sorted((envelope.get("meta") or {}).keys()),
        }
        if extra:
            record.update(extra)
        report["steps"].append(record)
        return record

    def called(name: str, envelope: dict, *, extra: dict | None = None) -> None:
        report["calls"].append({"tool": name, "ok": bool(envelope.get("ok"))})

    instance_from_tool: dict[str, Any] = {}
    commit_issued = False

    # ---- the request: display values from the live profile, never typed blind ---
    request_controls = dict(REQUEST_CONTROLS)

    def _profile_row(control_id: str, value: Any) -> dict[str, Any]:
        control = H.control_in(target.profile, control_id)
        law = dict(control.get("law") or {})
        row = {
            "declared_index": (control.get("binding") or {}).get("index"),
            "type": control.get("type"),
            "unit": control.get("unit"),
            "range": dict(control.get("range") or {}),
            "law_kind": law.get("kind"),
            "enum_values": [m.get("id") for m in control.get("enum_values") or []],
            "why": WHY.get(control_id),
        }
        if row["type"] == "enum":
            assert str(value) in row["enum_values"], (
                f"{control_id}: {value!r} is not a measured enum member of {row['enum_values']}"
            )
        if row["range"] and row["type"] == "number":
            low, high = row["range"].get("min"), row["range"].get("max")
            assert float(low) <= float(value) <= float(high), (
                f"{control_id}: {value} outside the live range {row['range']}"
            )
        row["value_sent"] = value
        row["expected_write_space"] = V.display_to_write_space(control, value)
        return row

    # the glide value depends on both the live range and the measured sweep model,
    # so it is derived before the rows are built - never hard-coded
    portamento_control = H.control_in(target.profile, "portamento_time")
    portamento_range = dict(portamento_control.get("range") or {})
    portamento = _portamento_display(float(portamento_range["min"]), float(portamento_range["max"]))
    request_controls["portamento_time"] = portamento["display"]

    for control_id, value in request_controls.items():
        report["profile"][control_id] = _profile_row(control_id, value)
    report["profile"]["portamento_time"]["measured_model"] = portamento
    report["profile"]["env_1_attack"]["measured_model"] = {
        "seconds_expected": float(REQUEST_CONTROLS["env_1_attack"]) ** ENV_TIME_POWER,
        "power": ENV_TIME_POWER,
        "source": ENV_TIME_MODEL["source"],
    }
    report["profile"]["env_1_release"]["measured_model"] = {
        "seconds_expected": float(REQUEST_CONTROLS["env_1_release"]) ** ENV_TIME_POWER,
        "power": ENV_TIME_POWER,
        "source": ENV_TIME_MODEL["source"],
    }
    report["request"] = {"label": "p13-808-glide-bass", "controls": dict(request_controls)}

    # ---- the host's own name/ident at each declared index (never assumed) ------
    for control_id, value in request_controls.items():
        row = report["profile"][control_id]
        control = H.control_in(target.profile, control_id)
        index = int(row["declared_index"])
        host_name = bridge.param_names(target.ref, [index])[0]
        ident = bridge.param_idents(target.ref, [index])[0]
        row.update(
            {
                "host_name_at_index": host_name,
                "host_name_matches_declared": bool(
                    host_name and B.normalise_name(host_name) in B.declared_names(control)
                ),
                "ident_now": ident,
                "ident_readable": ident is not None,
            }
        )
        assert row["host_name_matches_declared"] and row["ident_readable"], row

    # ---- the expected values, both ways ----------------------------------------
    for control_id, value in request_controls.items():
        row = report["profile"][control_id]
        control = H.control_in(target.profile, control_id)
        space = V.display_to_write_space(control, value)
        expected = {
            "display": value,
            "native_via_law": space["native"],
            "normalized_via_law": space["normalized"],
        }
        if control_id == "portamento_time":
            expected["normalized_via_measured_model"] = portamento["norm_target"]
        if control_id in ("env_1_attack", "env_1_release"):
            low, high = (control.get("range") or {}).get("min"), (control.get("range") or {}).get("max")
            expected["normalized_via_measured_model"] = (
                float(value) - float(low or 0.0)
            ) / (float(high) - float(low or 0.0))
        report["expected"][control_id] = expected

    # ---- the registered-surface calls ------------------------------------------
    instances = call(server, "plugin_instances", {})
    rows = (instances.get("data") or {}).get("instances") or []
    instance_from_tool = dict(rows[0]) if rows else {}
    called("plugin_instances", instances)
    step(
        "plugin_instances",
        instances,
        extra={
            "n_instances": len(rows),
            "instance_from_tool": instance_from_tool,
            "location_matches_target": (instance_from_tool.get("location") or {})
            == dict(target.ref.location),
        },
    )

    described = call(server, "plugin_describe", {"instance": instance_from_tool})
    described_data = described.get("data") or {}
    called("plugin_describe", described)
    step(
        "plugin_describe",
        described,
        extra={
            "counts": described_data.get("counts"),
            "profile": described_data.get("profile"),
            "state": described_data.get("state"),
        },
    )

    found = call(server, "plugin_find", {"instance": instance_from_tool, "query": "portamento"})
    found_data = found.get("data") or {}
    called("plugin_find", found)
    step(
        "plugin_find(query=portamento)",
        found,
        extra={"n_matches": found_data.get("n_matches"), "ids": [r.get("id") for r in found_data.get("matches") or []]},
    )

    control_ids = sorted(request_controls)
    before = call(server, "plugin_read", {"instance": instance_from_tool, "controls": control_ids})
    called("plugin_read(before)", before)
    before_values = (before.get("data") or {}).get("values") or {}
    report["controls_before"] = before_values
    step("plugin_read(before)", before, extra={"problems": (before.get("data") or {}).get("problems")})

    # ---- dry run: explicit, and zero verbs at the host boundary ----------------
    mark = counter.mark()
    preview = call(
        server,
        "plugin_write",
        {"instance": instance_from_tool, "request": report["request"], "dry_run": True},
    )
    dry_calls = counter.since(mark)
    called("plugin_write(dry_run=true)", preview)
    report["dry_run"] = {
        "ok": bool(preview.get("ok")),
        "would_write": (preview.get("data") or {}).get("would_write"),
        "committed": (preview.get("data") or {}).get("committed"),
        "verb_calls": dry_calls,
        "n_verb_calls": len(dry_calls),
        "error": (preview.get("error") or {}).get("code"),
        "plan_summary": (preview.get("data") or {}).get("plan_summary"),
        "plan": (preview.get("data") or {}).get("plan"),
        "outcome": (preview.get("data") or {}).get("outcome"),
    }
    step("plugin_write(dry_run=true)", preview, extra={"verb_calls": dry_calls})

    # ---- the one committed batch ------------------------------------------------
    if report["dry_run"]["ok"] and report["dry_run"]["would_write"] is True:
        mark = counter.mark()
        commit_issued = True
        committed = call(
            server,
            "plugin_write",
            {
                "instance": {"location": dict(target.ref.location)},
                "request": report["request"],
                "dry_run": False,
            },
        )
        commit_calls = counter.since(mark)
        called("plugin_write(dry_run=false)", committed)
        report["commit"] = {
            "ok": bool(committed.get("ok")),
            "committed": (committed.get("data") or {}).get("committed"),
            "error": (committed.get("error") or {}).get("code"),
            "error_detail": (committed.get("error") or {}).get("detail"),
            "outcome": (committed.get("data") or {}).get("outcome"),
            "plan": (committed.get("data") or {}).get("plan"),
            "plan_summary": (committed.get("data") or {}).get("plan_summary"),
            "host_verbs": commit_calls,
            "n_host_verbs": len(commit_calls),
            "read_cost_next": (committed.get("data") or {}).get("read_cost_next"),
        }
        step("plugin_write(dry_run=false)", committed, extra={"verb_calls": commit_calls})
        # the write record is written immediately: if a probe defect strikes during
        # the post-write measurement, the measured commit must still be readable
        commit_path = H.write_run_json(
            "p13_bass_commit",
            {
                "probe": "p13_bass_acceptance",
                "utc": report["utc"],
                "request": report["request"],
                "target": report["target"],
                "baseline": report["baseline"],
                "controls_before": report["controls_before"],
                "dry_run": report["dry_run"],
                "commit": report["commit"],
                "expected": report["expected"],
                "profile": report["profile"],
            },
            H.EVIDENCE_DIR,
        )
        print(f"\n[p13 bass] commit record: {commit_path}")
    else:
        report["blocker"] = "dry_run_did_not_authorise_the_write"
        report["refusals"].append(
            {
                "reason": "dry_run_did_not_authorise_the_write",
                "detail": "the preview did not report would_write=true; no committed write was issued",
                "envelope": preview,
            }
        )
        step("plugin_write(dry_run=false)", {"ok": False, "error": {"code": "not_attempted"}})

    # ---- independent verification ------------------------------------------------
    if commit_issued:
        vector_after, slice_after, document_after = _live_vectors(bridge, target)
        chunk_after = bridge.read_state_chunk(target.ref)
        revision_after = StateRevision.compute(
            state_channel=slice_after.block_text, parameters=vector_after
        )
        leaves_after = H.document_leaves_diff(
            document_after, baseline.document_snapshot, declared_derived=baseline.derived_leaves
        )
        moved = H.compare_parameters(baseline.parameters, vector_after, tolerance=0.0)
        moved_idents = sorted(row["ident"] for row in moved["mismatched"])
        expected_idents = sorted(
            str(report["profile"][cid]["ident_now"]) for cid in request_controls
        )
        read_after = call(
            server, "plugin_read", {"instance": instance_from_tool, "controls": control_ids}
        )
        called("plugin_read(after)", read_after)
        after_values = (read_after.get("data") or {}).get("values") or {}
        state_after = call(server, "plugin_state", {"instance": instance_from_tool, "op": "inspect"})
        called("plugin_state(after)", state_after)
        state_data = state_after.get("data") or {}

        per_control: dict[str, Any] = {}
        for control_id in request_controls:
            observed = dict(after_values.get(control_id) or {})
            expected = report["expected"][control_id]
            normalized = observed.get("normalized")
            row = {
                "value_sent": request_controls[control_id],
                "observed": observed,
                "display_agrees": observed.get("display") == request_controls[control_id],
                "normalized_delta_vs_law": (
                    None if normalized is None else float(normalized) - float(expected["normalized_via_law"])
                ),
            }
            if expected.get("normalized_via_measured_model") is not None and normalized is not None:
                row["normalized_delta_vs_measured_model"] = float(normalized) - float(
                    expected["normalized_via_measured_model"]
                )
            per_control[control_id] = row

        env_attack = per_control["env_1_attack"]["observed"].get("normalized")
        env_release = per_control["env_1_release"]["observed"].get("normalized")
        portamento_norm = per_control["portamento_time"]["observed"].get("normalized")
        env_max = float(report["profile"]["env_1_attack"]["range"]["max"])

        report["after"] = {
            "chunk_digest": text_digest(chunk_after),
            "block_digest": slice_after.block_digest,
            "revision": revision_after.digest,
            "revision_moved": revision_after.digest != baseline.revision.digest,
            "outside_span": H.outside_span_is_intact(
                baseline.chunk, chunk_after, baseline.slice, slice_after
            ),
            "document_leaves": {
                key: value
                for key, value in leaves_after.items()
                if key not in ("mismatched", "new_paths_sample", "derived_mismatched")
            },
            # the rows themselves, not only the count: the P12 run recorded
            # `n_mismatched: 1` and dropped the path, which made the measured drift
            # impossible to name afterwards (defect fixed there, applied here)
            "document_leaves_mismatched": leaves_after["mismatched"],
            "document_leaves_derived": leaves_after["derived_mismatched"],
            "moved_idents": moved_idents,
            "expected_moved_idents": expected_idents,
            "unexpected_moved": sorted(set(moved_idents) - set(expected_idents)),
            "expected_not_moved": sorted(set(expected_idents) - set(moved_idents)),
            "n_parameters_compared": moved["compared"],
            "per_control": per_control,
            "interpretation": {
                "env_1_attack_seconds": (
                    None if env_attack is None else (float(env_attack) * env_max) ** ENV_TIME_POWER
                ),
                "env_1_release_seconds": (
                    None if env_release is None else (float(env_release) * env_max) ** ENV_TIME_POWER
                ),
                "portamento_seconds": (
                    None
                    if portamento_norm is None
                    else 2.0 ** (-10.0 + 14.0 * float(portamento_norm))
                ),
                "filter_1_cutoff_hz": 440.0
                * 2.0 ** ((float(request_controls["filter_1_cutoff"]) - 69.0) / 12.0),
                "env_model_source": ENV_TIME_MODEL["source"],
            },
            "state_after": {
                "settings_keys": ((state_data.get("state") or {}).get("settings_keys")),
                "routes_total": ((state_data.get("state") or {}).get("routes_total")),
                "n_active_routes": ((state_data.get("state") or {}).get("n_active_routes")),
                "slice_block_digest": ((state_data.get("slice") or {}).get("block_digest")),
            },
        }

        report["verdict"] = (
            "ALL_VERIFIED"
            if report["commit"] and report["commit"]["ok"]
            and (report["commit"]["outcome"] or {}).get("verdict") == "ALL_VERIFIED"
            else "COMMIT_NOT_VERIFIED"
        )

        # ---- the manual-restore record (the sound stays; this is how to undo it) ----
        report["restore_reference"] = {
            "note": (
                "the sound is the deliverable and is left in place; these captured display "
                "values are what a manual restore would write back through the same tool"
            ),
            "controls": {
                control_id: {
                    "display_to_write_back": (before_values.get(control_id) or {}).get("display"),
                    "normalized_at_baseline": (before_values.get(control_id) or {}).get("normalized"),
                }
                for control_id in request_controls
            },
        }

    path = H.write_run_json("p13_bass_acceptance", report, H.EVIDENCE_DIR)
    report["evidence"] = str(path)
    print(f"\n[p13 bass] evidence: {path}")

    # ---- assertions last: the measurement is already in the artifact ------------
    assert not report["blocker"], f"blocker={report['blocker']} refusals={report['refusals']}"
    steps = {row["step"]: row for row in report["steps"]}
    assert steps["plugin_instances"]["ok"], steps["plugin_instances"]
    assert steps["plugin_describe"]["ok"], steps["plugin_describe"]
    assert steps["plugin_read(before)"]["ok"], steps["plugin_read(before)"]
    assert report["dry_run"]["ok"] and report["dry_run"]["would_write"] is True, report["dry_run"]
    assert report["dry_run"]["n_verb_calls"] == 0, (
        f"the dry run must write nothing at the host boundary: {report['dry_run']['verb_calls']}"
    )
    assert report["commit"]["ok"], report["commit"]
    assert (report["commit"].get("outcome") or {}).get("verdict") == "ALL_VERIFIED", report["commit"]
    assert report["commit"]["n_host_verbs"] >= 1, "a committed batch must issue at least one write verb"
    after = report["after"]
    assert after["outside_span"]["intact"], after["outside_span"]
    assert after["document_leaves"]["compared"] > 0, (
        "the document comparison must compare real leaves, or it proves nothing"
    )
    assert not after["unexpected_moved"], (
        f"only the written controls may move: unexpected={after['unexpected_moved'][:10]}"
    )
    assert not after["expected_not_moved"], (
        f"every written control must move: not moved={after['expected_not_moved'][:10]}"
    )
    for control_id, row in after["per_control"].items():
        assert row["observed"].get("display") is not None or row["observed"].get("display_note"), (
            f"{control_id}: the read-back must report a display or the reason it has none"
        )
        if row["normalized_delta_vs_law"] is not None:
            assert abs(row["normalized_delta_vs_law"]) <= TOLERANCE_NORMALIZED, (
                f"{control_id}: read-back disagrees with the declared law by "
                f"{row['normalized_delta_vs_law']}"
            )
        if "normalized_delta_vs_measured_model" in row:
            assert abs(row["normalized_delta_vs_measured_model"]) <= 0.002, (
                f"{control_id}: read-back disagrees with the measured sweep model by "
                f"{row['normalized_delta_vs_measured_model']}"
            )
    interpretation = after["interpretation"]
    assert interpretation["env_1_attack_seconds"] is not None
    assert abs(interpretation["env_1_attack_seconds"] - 0.00390625) / 0.00390625 < 0.05, interpretation
    assert abs(interpretation["env_1_release_seconds"] - 0.1478) / 0.1478 < 0.05, interpretation
    assert abs(interpretation["portamento_seconds"] - PORTAMENTO_TARGET_SECONDS) / (
        PORTAMENTO_TARGET_SECONDS
    ) < 0.05, interpretation
