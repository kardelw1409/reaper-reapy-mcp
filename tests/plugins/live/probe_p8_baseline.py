"""P8.1 baseline probe for the live Vital instance - **READ ONLY**.

Establishes, before any live write is permitted:

1. the identity of the active project's track and the addressed instance
   (name, class id, module, parameter count, slot correlation, GUID presence);
2. the targeted state slice (span, digest, and that every byte outside it is
   intact), the decoded document, the live `synth_version` and the route table;
3. the complete **recovery set**: the full normalized parameter vector plus the
   exact block text (written to `recovery/`, never into the evidence file, so the
   evidence stays small and the recovery data stays byte-exact), with the
   revision digest the engine computes over them;
4. the **live binding table** (P8.2): for every declared control index, the
   host's parameter name and ident, whether the declared name was confirmed, and
   the concrete refusal reason where it was not;
5. measured costs: the full-vector capture and the binding reads.

It performs no write. It refuses (exit 2) when the host is unreachable or when
the instance cannot be resolved, and never falls back to a mutation.

Run: `py -3.11 tests/plugins/live/probe_p8_baseline.py`
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
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reaper_reapy_mcp.plugins import binding as B  # noqa: E402
from reaper_reapy_mcp.plugins import model as M  # noqa: E402
from reaper_reapy_mcp.plugins.errors import NotConnected, PluginError  # noqa: E402
from reaper_reapy_mcp.plugins.host import reaper as R  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # so `live_harness` is importable when run as a script
    sys.path.insert(0, str(HERE))

from live_harness import (  # noqa: E402 - P32: run artifacts, never the repo
    EVIDENCE_DIR,
    RUN_DIR,
    write_run_json,
)

#: A NEW recovery point goes to the run directory (class C, P32). The historical
#: points were removed from the repository in P33 and live in the external research
#: archive; `restore_p8.py` finds them through `REAPER_MCP_RECOVERY_SEARCH`.
RECOVERY_DIR = RUN_DIR / "recovery"

#: The controls whose write behaviour the live phase will use. Chosen from the
#: pinned registry rows: affine (cutoff/volume/feedback/delay), enum (blend) and
#: one `none` law control, so the phase can demonstrate a write, an enum write
#: and a typed conversion refusal.
SHORTLIST = {
    "filter_1_cutoff": 104,
    "volume": 462,
    "chorus_feedback": 5,
    "chorus_delay_1": 2,
    "chorus_cutoff": 1,
    "distortion_filter_blend": 40,
    "lfo_1_frequency": 157,
    "osc_1_unison_detune": 395,
}


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main() -> int:
    report: dict = {"utc": _utc(), "probe": "p8_baseline", "read_only": True, "steps": []}
    bridge = R.ReaperHostBridge(timeout=30.0)
    try:
        tracks = bridge.list_tracks()
    except (NotConnected, PluginError) as exc:
        report["refused"] = f"host not reachable: {exc}"
        report["execute"] = False
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    provider = VitalProvider()
    found = None
    for track in tracks:
        for fx_index in range(int(track.n_fx)):
            facts = bridge.fx_facts(track.track_index, fx_index)
            if provider.matches(facts):
                found = (track, fx_index, facts)
                break
        if found:
            break
    if found is None:
        report["refused"] = "no Vital VST3 instance is present in the open project"
        report["execute"] = False
        report["tracks"] = [
            {"track_index": t.track_index, "name": t.name, "n_fx": t.n_fx} for t in tracks
        ]
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    track, fx_index, facts = found
    ref = M.PluginInstanceRef(
        host="reaper",
        location={"track_index": track.track_index, "fx_index": fx_index},
        provider="vital",
        identity=provider.identify(facts),
    )
    report["project"] = {
        "n_tracks": len(tracks),
        "track_index": track.track_index,
        "track_name": track.name,
        "fx_index": fx_index,
        "n_fx_on_track": track.n_fx,
    }
    report["identity"] = {
        "fx_name": facts.fx_name,
        "class_id": facts.class_id,
        "module": facts.module,
        "param_count": facts.param_count,
        "version": facts.version,
        "matching_fx_count": facts.fx_count,
        "guid_present": bool(facts.session_handle),
        "guid_sha256_prefix": hashlib.sha256(str(facts.session_handle).encode()).hexdigest()[:12]
        if facts.session_handle
        else None,
    }

    # -- state slice and document ------------------------------------------
    started = time.perf_counter()
    chunk = bridge.read_state_chunk(ref)
    slice_ = bridge.locate_state_slice(ref, facts, chunk)
    report["slice"] = {
        "line_start": slice_.line_start,
        "line_end": slice_.line_end,
        "lines": slice_.line_end - slice_.line_start + 1,
        "block_digest": slice_.block_digest,
        "chunk_digest": slice_.chunk_digest,
        "outside_bytes_intact": R.splice_block_text(chunk, slice_, slice_.block_text) == chunk,
        "value": f"{facts.fx_name} @ track {track.track_index} fx {fx_index}",
    }
    codec = provider.codec()
    view = codec.decode(slice_)
    active_routes = [row for row in view["routes"] if row["destination"]]
    report["document"] = {
        "synth_version": view["synth_version"],
        "preset_name": view["preset_name"],
        "n_slots_total": len(view["routes"]),
        "n_active_routes": len(active_routes),
        "active_routes": active_routes[:8],
        "top_level_keys": sorted(view["document"].keys())[:20],
        "settings_key_count": len(view["settings"]),
    }

    # -- the recovery set: full parameter vector ---------------------------
    count = int(facts.param_count or 0)
    started = time.perf_counter()
    idents = bridge.param_idents(ref, list(range(count)))
    idents_seconds = time.perf_counter() - started
    started = time.perf_counter()
    values = bridge.read_params(ref, list(range(count)))
    values_seconds = time.perf_counter() - started
    missing = [index for index, ident in enumerate(idents) if not ident]
    vector = {str(ident): float(value) for ident, value in zip(idents, values) if ident}
    revision = M.StateRevision.compute(state_channel=slice_.block_text, parameters=vector)
    report["recovery_set"] = {
        "param_count": count,
        "idents_read": len(idents),
        "idents_missing": len(missing),
        "values_read": len(values),
        "vector_size": len(vector),
        "parameter_capture_seconds": {
            "idents": round(idents_seconds, 4),
            "values": round(values_seconds, 4),
            "total": round(idents_seconds + values_seconds, 4),
        },
        "block_text_chars": len(slice_.block_text),
        "base_revision_digest": revision.digest,
        "complete": bool(count and len(vector) == count and len(values) == count),
    }

    # -- the live binding table (P8.2) -------------------------------------
    profile = provider.build_profile(provider.identify(facts))
    indexed = B.indexed_controls(profile)
    started = time.perf_counter()
    names = bridge.param_names(ref, [index for _cid, index in indexed])
    idents_for_index = bridge.param_idents(ref, [index for _cid, index in indexed])
    binding_seconds = time.perf_counter() - started
    _bound, binding_report = B.bind_profile(
        profile, indexed=indexed, names=names, idents=idents_for_index
    )
    index_of = {control_id: index for control_id, index in indexed}
    shortlist_rows = []
    for control_id, index in SHORTLIST.items():
        control = next((row for row in profile["controls"] if row["id"] == control_id), None)
        ident = None
        for position, (cid, idx) in enumerate(indexed):
            if cid == control_id:
                ident = idents_for_index[position]
                break
        row: dict = {
            "control": control_id,
            "index": index_of.get(control_id, index),
            "declared_names": sorted(B.declared_names(control)) if control else [],
            "live_name": names[indexed.index((control_id, index_of.get(control_id, index)))]
            if (control_id, index_of.get(control_id, index)) in indexed
            else None,
            "live_ident": ident,
            "type": (control or {}).get("type"),
            "unit": (control or {}).get("unit"),
            "range": (control or {}).get("range"),
            "law": ((control or {}).get("law") or {}).get("kind"),
            "declared_ident": ((control or {}).get("binding") or {}).get("ident"),
        }
        position = index_of.get(control_id)
        if position is not None:
            api_index = position
            row["live_normalized_now"] = float(
                bridge.read_params(ref, [api_index])[0]
            )
        shortlist_rows.append(row)
    report["binding_table"] = {
        "controls_indexed": binding_report.controls_indexed,
        "bound": binding_report.bound,
        "declared_ident_kept": binding_report.declared_ident_kept,
        "ident_refreshed": binding_report.ident_refreshed,
        "refused_reasons": binding_report.refused_reasons,
        "unverified_sample": binding_report.unverified[:10],
        "read_seconds": round(binding_seconds, 4),
        "shortlist": shortlist_rows,
    }

    # -- recovery artifacts (byte-exact block + full vector) ---------------
    stamp = _run_stamp()
    recovery_payload = {
        "probe": "p8_recovery",
        "utc": _utc(),
        "read_only": True,
        "identity": report["identity"],
        "location": dict(ref.location),
        "slice": {"line_start": slice_.line_start, "line_end": slice_.line_end,
                  "block_digest": slice_.block_digest, "chunk_digest": slice_.chunk_digest},
        "block_text": slice_.block_text,
        "parameters": vector,
        "synth_version": view["synth_version"],
    }
    recovery_path = write_run_json("p8_recovery", recovery_payload, RECOVERY_DIR)
    report["recovery_artifact"] = {
        "path": str(recovery_path),
        "sha256": hashlib.sha256(recovery_path.read_bytes()).hexdigest(),
    }

    path = write_run_json("p8_baseline", report, EVIDENCE_DIR)
    print(
        json.dumps(
            {
                "evidence": str(path),
                "recovery": str(recovery_path),
                "identity": report["identity"],
                "slice_lines": report["slice"]["lines"],
                "outside_bytes_intact": report["slice"]["outside_bytes_intact"],
                "synth_version": report["document"]["synth_version"],
                "recovery_complete": report["recovery_set"]["complete"],
                "capture_seconds": report["recovery_set"]["parameter_capture_seconds"],
                "binding": {
                    "indexed": binding_report.controls_indexed,
                    "bound": binding_report.bound,
                    "reasons": binding_report.refused_reasons,
                    "seconds": report["binding_table"]["read_seconds"],
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
