"""P8 recovery tool: restore the live instance from a captured recovery artifact.

The recovery set written by `probe_p8_baseline.py` (byte-exact block text + the
full parameter vector) is the way back for any P8 live run that ends without a
verified restore. This script:

1. reads a recovery artifact (newest by default, an explicit path with `--file`);
2. reads the live instance and reports what currently differs from the artifact
   (document leaves, parameters, chunk digest) - the *state of each channel
   before* anything is written;
3. writes the captured document leaves back through the transaction pipeline and
   verifies the state channel by comparison;
4. compares the full parameter vector and repairs it through the transport when
   it differs, then re-verifies;
5. re-reads and reports every channel again (document, parameters, chunk digest)
   and states plainly whether the instance matches the captured recovery set.

It never guesses: an artifact whose identity does not match the live instance is
a refusal, and a channel that cannot be compared is reported as such.

Run: `py -3.11 tests/plugins/live/restore_p8.py [--file recovery.json] [--dry-run]`
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
for _path in (str(SRC), str(REPO_ROOT / "tests" / "plugins")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import live_harness as H  # noqa: E402

from reaper_reapy_mcp.plugins.errors import PluginError  # noqa: E402
from reaper_reapy_mcp.plugins.host import reaper as R  # noqa: E402
from reaper_reapy_mcp.plugins.model import text_digest  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import VitalProvider  # noqa: E402
from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402
from reaper_reapy_mcp.plugins.transaction import Transaction  # noqa: E402


def _latest_recovery() -> Path:
    files = sorted(H.RECOVERY_DIR.glob("p8_recovery__*.json"))
    if not files:
        raise SystemExit("no recovery artifact exists under tests/plugins/live/recovery/")
    return files[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None, help="recovery artifact (default: newest)")
    parser.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    args = parser.parse_args()

    path = Path(args.file) if args.file else _latest_recovery()
    artifact = json.loads(path.read_text(encoding="utf-8"))
    captured_parameters = {str(k): float(v) for k, v in artifact["parameters"].items()}
    report: dict = {
        "probe": "p8_restore",
        "utc": H.utc_now(),
        "artifact": str(path),
        "artifact_utc": artifact.get("utc"),
        "dry_run": bool(args.dry_run),
        "target": artifact.get("identity"),
    }
    print(f"recovery artifact: {path} (captured {artifact.get('utc')})")

    bridge = R.ReaperHostBridge(timeout=60.0)
    provider = VitalProvider()
    try:
        target = H.resolve_target(bridge, provider)
    except PluginError as exc:
        print(f"REFUSED: cannot resolve the instance: {exc.code}: {exc.message}")
        return 2

    if artifact.get("identity", {}).get("class_id") not in (None, target.facts.class_id):
        print("REFUSED: the artifact was captured for a different build")
        return 2

    chunk, document = (
        bridge.read_state_chunk(target.ref),
        None,
    )
    facts = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    slice_ = bridge.locate_state_slice(target.ref, facts, chunk)
    document = target.codec.decode(slice_)
    snapshot = {"document": artifact_document(artifact), "root": VC.MUTABLE_ROOT}

    leaves = H._settings_leaves(document)
    # The provider declares leaves the plugin re-serializes itself. They are
    # excluded from the restore *request* (the codec refuses them as write
    # targets, so a request that included one would be refused wholesale), and
    # they are named as a gap so "restored" never overstates what came back.
    declared_derived = [
        str(path) for path in ((target.profile.get("state") or {}).get("derived_leaves") or [])
    ]
    captured_leaves = {
        path: value
        for path, value in H._settings_leaves(snapshot).items()
        if path not in set(declared_derived)
    }
    document_diff = H.document_leaves_diff(
        document, snapshot, declared_derived=declared_derived
    )

    count = int(facts.param_count or 0)
    idents = bridge.param_idents(target.ref, list(range(count)))
    values = bridge.read_params(target.ref, list(range(count)))
    observed = {str(i): float(v) for i, v in zip(idents, values) if i}
    parameter_diff = H.compare_parameters(captured_parameters, observed, tolerance=0.0)

    report["before"] = {
        "chunk_digest": text_digest(chunk),
        "chunk_digest_matches_artifact": text_digest(chunk) == artifact["slice"]["chunk_digest"],
        "block_digest_matches_artifact": slice_.block_digest == artifact["slice"]["block_digest"],
        "active_routes": [row for row in document["routes"] if row.get("destination")],
        "document_leaves": {k: v for k, v in document_diff.items() if k not in ("mismatched", "new_paths_sample")},
        "document_mismatched_sample": document_diff["mismatched"][:10],
        "parameters": {k: v for k, v in parameter_diff.items() if k != "mismatched"},
        "parameter_mismatched_sample": parameter_diff["mismatched"][:10],
    }
    report["restore_plan"] = H.restore_completeness(
        channels_verified=document_diff["all_equal"],
        declared_derived=declared_derived,
    )
    print(json.dumps(report["before"], indent=2, sort_keys=True, default=str)[:4000])
    print("restore plan:", json.dumps(report["restore_plan"], sort_keys=True, default=str))

    if args.dry_run:
        report["result"] = "dry run: nothing written"
        path = H.write_run_json("p8_restore", report, H.EVIDENCE_DIR)
        print(f"evidence: {path}")
        return 0

    # 1. state channel: write every captured settings leaf back
    restore_tx = Transaction(
        provider=target.provider,
        codec=target.codec,
        bridge=bridge,
        profile=target.profile,
        ref=target.ref,
        request={"state": dict(captured_leaves)},
        token=target.token,
        registry=target.registry,
        dry_run=False,
    )
    result = restore_tx.run()
    report["state_write"] = {
        "ok": result.ok,
        "verdict": str(result.outcome.verdict.value),
        "host": result.outcome.host.as_dict(),
        "n_rows": len(result.outcome.channels["state"].rows),
        "n_mismatched": len(result.outcome.channels["state"].mismatched),
        "error": result.error.to_dict() if result.error else None,
    }
    print("state restore:", json.dumps(report["state_write"], sort_keys=True, default=str)[:1200])

    # 2. parameters: compare, repair through the transport if needed
    chunk2 = bridge.read_state_chunk(target.ref)
    facts2 = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    document2 = target.codec.decode(bridge.locate_state_slice(target.ref, facts2, chunk2))
    idents2 = bridge.param_idents(target.ref, list(range(count)))
    values2 = bridge.read_params(target.ref, list(range(count)))
    observed2 = {str(i): float(v) for i, v in zip(idents2, values2) if i}
    diff2 = H.compare_parameters(captured_parameters, observed2, tolerance=0.0)
    report["parameters_after_state_restore"] = {
        k: v for k, v in diff2.items() if k != "mismatched"
    }
    if not diff2["all_equal"]:
        to_fix = {str(row["ident"]): float(row["expected"]) for row in diff2["mismatched"]}
        report["parameter_repair"] = H.restore_parameters(bridge, target.ref, to_fix)

    # 3. final per-channel report
    chunk3 = bridge.read_state_chunk(target.ref)
    facts3 = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    document3 = target.codec.decode(bridge.locate_state_slice(target.ref, facts3, chunk3))
    idents3 = bridge.param_idents(target.ref, list(range(count)))
    values3 = bridge.read_params(target.ref, list(range(count)))
    observed3 = {str(i): float(v) for i, v in zip(idents3, values3) if i}
    final_document = H.document_leaves_diff(document3, snapshot)
    final_parameters = H.compare_parameters(captured_parameters, observed3, tolerance=0.0)
    report["final"] = {
        "chunk_digest": text_digest(chunk3),
        "chunk_matches_artifact": text_digest(chunk3) == artifact["slice"]["chunk_digest"],
        "block_digest_matches_artifact": (
            target.codec is not None
            and text_digest(
                bridge.locate_state_slice(target.ref, facts3, chunk3).block_text
            )
            == artifact["slice"]["block_digest"]
        ),
        "active_routes": [row for row in document3["routes"] if row.get("destination")],
        "document_leaves": {
            k: v for k, v in final_document.items() if k not in ("mismatched", "new_paths_sample")
        },
        "parameters": {k: v for k, v in final_parameters.items() if k != "mismatched"},
        "restored": bool(
            final_document["all_equal"]
            and final_parameters["all_equal"]
            and not [row for row in document3["routes"] if row.get("destination")]
        ),
    }
    # One fact, one representation (Decision 2/5): the excluded derived leaf is
    # named as a gap, and completeness is reported separately from verification.
    report["final"]["completeness"] = H.restore_completeness(
        channels_verified=report["final"]["restored"],
        declared_derived=declared_derived,
    )
    report["named_gaps"] = report["final"]["completeness"]["named_gaps"]
    path_written = H.write_run_json("p8_restore", report, H.EVIDENCE_DIR)
    print("final:", json.dumps(report["final"], indent=2, sort_keys=True, default=str)[:2000])
    print(f"evidence: {path_written}")
    return 0 if report["final"]["restored"] else 3


def artifact_document(artifact: dict) -> dict:
    """The captured document, decoded from the artifact's byte-exact block text."""
    framing = VC.parse_block_framing(artifact["block_text"])
    document, _brace = VC.decode_document(framing["B"])
    return document


if __name__ == "__main__":
    raise SystemExit(main())
