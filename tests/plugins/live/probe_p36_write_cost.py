"""P36 write-cost instrument: where the 20+ seconds of an ident-path write go.

**LIVE WRITE PROBE - never collected by pytest; run explicitly.**

Measures one `plugin_write` end to end on an isolated instance pair (Vital =
ident-addressed provider path, Surge XT = providerless host-scope path) with the
same scenario shapes: one control (real + dry run) and four controls (real + dry
run). Instrumentation is *outside* the implementation: bridge verbs are wrapped
for call counts + seconds, and the reapy API module the bridge returns from
`_api()` is proxied so every `TrackFX_*` call is counted. Nothing in
`src/` knows the probe exists.

Safety: writes only the control values it first read, restores them in the same
run and re-reads the restoration; a failed restore stops the run immediately
with the exception recorded. The project is never saved.

Run::

    py -3.11 tests/plugins/live/probe_p36_write_cost.py --track 11 --vital-fx 0 --surge-fx 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE.parent
for _path in (str(SRC), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from reaper_reapy_mcp.plugins import api  # noqa: E402

from live_harness import finalize_run  # noqa: E402 - run artifacts, never the repo (P32)

#: Every inner host call this probe counts (the names the audit reasoned about).
_TRACKED_API = (
    "TrackFX_GetParamIdent",
    "TrackFX_GetParamName",
    "TrackFX_GetParamNormalized",
    "TrackFX_GetParamFromIdent",
    "TrackFX_SetParamNormalized",
    "TrackFX_GetFXName",
    "TrackFX_GetCount",
    "TrackFX_GetNamedConfigParm",
    "TrackFX_FormatParamValueNormalized",
    "TrackFX_GetParameterStepSizes",
    "TrackFX_GetParamEx",
    "GetTrackStateChunk",
    "GetTrack",
)

_INNER_CALLS: dict[str, int] = {}


class _ApiProxy:
    """Counts the tracked functions; everything else passes through."""

    def __init__(self, module: Any) -> None:
        self._module = module

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._module, name)
        if name not in _TRACKED_API or not callable(attr):
            return attr

        def counted(*args: Any, **kwargs: Any) -> Any:
            _INNER_CALLS[name] = _INNER_CALLS.get(name, 0) + 1
            return attr(*args, **kwargs)

        return counted


def _instrument(bridge: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wrap the bridge's read/write verbs for (calls, seconds, inner deltas)."""
    verbs: dict[str, Any] = {}
    originals: dict[str, Any] = {}
    verb_names = (
        "list_tracks",
        "fx_facts",
        "param_idents",
        "param_names",
        "read_params",
        "read_state_chunk",
        "locate_state_slice",
        "write_params",
        "write_params_by_index",
        "format_params_normalized",
        "param_metadata",
    )
    for name in verb_names:
        fn = getattr(bridge, name, None)
        if not callable(fn):
            continue
        originals[name] = fn
        entry: dict[str, Any] = {"calls": 0, "seconds": 0.0, "inner": {}}
        verbs[name] = entry

        def make(_name: str, _fn: Any, _entry: dict[str, Any]) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                before = dict(_INNER_CALLS)
                started = time.perf_counter()
                try:
                    return _fn(*args, **kwargs)
                finally:
                    dt = time.perf_counter() - started
                    _entry["calls"] += 1
                    _entry["seconds"] = round(_entry["seconds"] + dt, 4)
                    delta = {
                        key: _INNER_CALLS[key] - before.get(key, 0)
                        for key in _INNER_CALLS
                        if _INNER_CALLS[key] != before.get(key, 0)
                    }
                    for key, value in delta.items():
                        _entry["inner"][key] = _entry["inner"].get(key, 0) + value

            return wrapper

        setattr(bridge, name, make(name, fn, entry))
    original_api = bridge._api
    bridge._api = lambda: _ApiProxy(original_api())
    return verbs, originals


def _delta(counter: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {
        key: counter.get(key, 0) - before.get(key, 0)
        for key in set(counter) | set(before)
        if counter.get(key, 0) != before.get(key, 0)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", type=int, default=11)
    parser.add_argument("--vital-fx", type=int, default=0)
    parser.add_argument("--surge-fx", type=int, default=1)
    parser.add_argument("--phase", default="unlabeled", help="a label for the measurement (before/after)")
    args = parser.parse_args()

    config = api.PluginConfig.from_env()
    bridge = api.session_bridge(config)
    verbs, _originals = _instrument(bridge)

    vital = {"track_index": args.track, "fx_index": args.vital_fx}
    surge = {"track_index": args.track, "fx_index": args.surge_fx}

    payload: dict[str, Any] = {
        "phase": args.phase,
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers": list(config.provider_ids),
        "locations": {"vital": vital, "surge": surge},
        "scenarios": [],
        "restores": [],
    }

    def read_normalized(instance: dict[str, Any], controls: list[str]) -> dict[str, float]:
        envelope = api.plugin_read(config=config, instance=instance, controls=controls)
        if envelope.get("ok") is not True:
            # Providerless instances need a prepared artifact; reuse serves the stored
            # one (P34 measured it), a prepare would measure from scratch.
            api.plugin_prepare(config=config, instance=instance, mode="reuse")
            envelope = api.plugin_read(config=config, instance=instance, controls=controls)
        values = (envelope.get("data") or {}).get("values") or {}
        out: dict[str, float] = {}
        for control in controls:
            row = values.get(control) or {}
            out[control] = float(row.get("normalized"))
        return out

    def pick_controls(instance: dict[str, Any], query: str, count: int) -> list[str]:
        envelope = api.plugin_find(config=config, instance=instance, query=query, limit=24)
        matches = (envelope.get("data") or {}).get("matches") or []
        picked = [str(row.get("id")) for row in matches if row.get("kind") == "number" and row.get("id")]
        if len(picked) < count:
            raise RuntimeError(f"find({query!r}) offered {len(picked)} numeric controls for {instance}")
        return picked[:count]

    def run_scenario(
        name: str,
        instance: dict[str, Any],
        controls: dict[str, float],
        *,
        dry_run: bool,
        restore_to: dict[str, float] | None,
    ) -> dict[str, Any]:
        inner_before = dict(_INNER_CALLS)
        verbs_before = {key: dict(value) for key, value in verbs.items()}
        started = time.perf_counter()
        envelope = api.plugin_write(
            config=config,
            instance=instance,
            request={
                "controls": {control: {"normalized": value} for control, value in controls.items()},
                "label": f"p36 {name}",
            },
            dry_run=dry_run,
        )
        seconds = time.perf_counter() - started
        data = envelope.get("data") or {}
        outcome = envelope.get("outcome") or {}
        inner = _delta(_INNER_CALLS, inner_before)
        verb_delta = {
            key: {
                "calls": value["calls"] - verbs_before.get(key, {}).get("calls", 0),
                "seconds": round(value["seconds"] - verbs_before.get(key, {}).get("seconds", 0.0), 4),
            }
            for key, value in verbs.items()
        }
        entry = {
            "name": name,
            "instance": instance,
            "dry_run": dry_run,
            "controls": controls,
            "requested": len(controls),
            "ok": bool(envelope.get("ok")),
            "committed": bool(data.get("committed")),
            "verdict": (outcome.get("verdict") or (data.get("outcome") or {}).get("verdict")),
            "error": (envelope.get("error") or {}).get("code") if envelope.get("ok") is False else None,
            "seconds": round(seconds, 3),
            "host": (outcome.get("host") or (data.get("outcome") or {}).get("host")),
            "inner_calls": inner,
            "inner_total": sum(inner.values()),
            "verbs": {key: value for key, value in verb_delta.items() if value["calls"]},
            "cache": ((envelope.get("meta") or {}).get("cache") or {}).get("source"),
        }
        payload["scenarios"].append(entry)
        print(
            f"[{name}] dry={dry_run} ok={entry['ok']} committed={entry['committed']} "
            f"verdict={entry['verdict']} seconds={entry['seconds']} inner={entry['inner_total']}"
        )
        if restore_to is not None:
            if not entry["committed"]:
                raise RuntimeError(f"scenario {name} did not commit; refusing to continue")
            restore_envelope = api.plugin_write(
                config=config,
                instance=instance,
                request={
                    "controls": {
                        control: {"normalized": value} for control, value in restore_to.items()
                    },
                    "label": f"p36 {name} restore",
                },
                dry_run=False,
            )
            restore_ok = bool(restore_envelope.get("ok")) and bool(
                (restore_envelope.get("data") or {}).get("committed")
            )
            reread = read_normalized(instance, sorted(restore_to))
            restored = all(abs(reread[k] - v) <= 1e-6 for k, v in restore_to.items())
            payload["restores"].append(
                {"scenario": name, "ok": restore_ok, "verified": restored, "reread": reread}
            )
            print(f"[{name}] restore ok={restore_ok} verified={restored}")
            if not (restore_ok and restored):
                raise RuntimeError(f"restore of {name} did not verify; stopping")
        return entry

    try:
        vital_single = pick_controls(vital, "filter_1_cutoff", 1)
        vital_batch = pick_controls(vital, "filter_1", 4)
        surge_single = pick_controls(surge, "a osc drift", 1)
        surge_batch = pick_controls(surge, "a osc", 4)
        payload["controls"] = {
            "vital_single": vital_single,
            "vital_batch": vital_batch,
            "surge_single": surge_single,
            "surge_batch": surge_batch,
        }

        # -- Vital (ident path) --------------------------------------------
        base = read_normalized(vital, vital_single)
        targets = {c: min(1.0, v + 0.010) for c, v in base.items()}
        run_scenario("vital_single_write", vital, targets, dry_run=False, restore_to=base)
        run_scenario("vital_single_dryrun", vital, targets, dry_run=True, restore_to=None)

        base_batch = read_normalized(vital, vital_batch)
        targets_batch = {c: min(1.0, v + 0.005) for c, v in base_batch.items()}
        run_scenario("vital_batch4_write", vital, targets_batch, dry_run=False, restore_to=base_batch)
        run_scenario("vital_batch4_dryrun", vital, targets_batch, dry_run=True, restore_to=None)

        # -- Surge XT (providerless path) ----------------------------------
        base_s = read_normalized(surge, surge_single)
        targets_s = {c: min(1.0, v + 0.010) for c, v in base_s.items()}
        run_scenario("surge_single_write", surge, targets_s, dry_run=False, restore_to=base_s)
        run_scenario("surge_single_dryrun", surge, targets_s, dry_run=True, restore_to=None)

        base_sb = read_normalized(surge, surge_batch)
        targets_sb = {c: min(1.0, v + 0.005) for c, v in base_sb.items()}
        run_scenario("surge_batch4_write", surge, targets_sb, dry_run=False, restore_to=base_sb)
        run_scenario("surge_batch4_dryrun", surge, targets_sb, dry_run=True, restore_to=None)

        payload["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the record must survive the failure
        payload["status"] = "failed"
        payload["exception"] = f"{type(exc).__name__}: {exc}"
        finalize_run(
            "p36_write_cost",
            payload,
            purpose="stage-level cost of one plugin_write per addressing path (P36)",
            findings=[payload["exception"]],
        )
        raise

    payload["totals"] = {
        "inner_calls_all": sum(_INNER_CALLS.values()),
        "inner_by_name": dict(sorted(_INNER_CALLS.items())),
    }
    evidence, report = finalize_run(
        "p36_write_cost",
        payload,
        purpose=(
            "stage-level host-call/time cost of one plugin_write per addressing path "
            "(Vital ident vs Surge XT providerless), single and 4-control"
        ),
        findings=[
            f"phase={args.phase}",
            f"vital single: {payload['scenarios'][0]['seconds']} s, "
            f"{payload['scenarios'][0]['inner_total']} inner calls",
            f"surge single: {payload['scenarios'][4]['seconds']} s, "
            f"{payload['scenarios'][4]['inner_total']} inner calls",
        ],
        directory=None,
    )
    print("evidence:", evidence)
    print("report:", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
