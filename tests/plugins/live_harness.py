"""The live-mutation harness: capture -> action -> verify -> restore -> verify restore.

Shared by the P8 live suites and by the offline tests that prove the harness can
**refuse to test**. Everything here is deliberately explicit about the four
things a live mutation must never assume (AUD-015, [19 Decision 1]):

- a target is resolved **uniquely** (zero or several candidates is a refusal, not
  a pick);
- the recovery set is the **complete** capture (document block + full parameter
  vector) or the harness refuses before the first write;
- the restore is a real write followed by a real read-back, and the comparison is
  per channel;
- what could not be compared is named, never counted as restored.

The harness itself performs no host call at import time and knows nothing about
a specific plugin: it talks to a `PluginProvider`, a `StateCodec` and a
`HostBridge`, exactly like the transaction engine.

**Storage policy (P32/P33).** This module is also the single place where a live run
*stores* its results, and since P32 that place is **outside the repository**:

* ``run_artifacts/<UTC stamp>__<task>__<pid>/evidence/`` holds the raw JSON (and,
  when a probe renders, the WAV/PNG beside it);
* ``run_artifacts/<run>/report.md`` is the short human-readable result and
  ``run.json`` its provenance manifest;
* ``LEGACY_EVIDENCE_DIR`` (``tests/plugins/live/evidence``) names the **historical
  corpus layout**. In P33 that corpus was removed from the repository and moved to
  the external research archive (``%LOCALAPPDATA%\reaper_reapy_mcp\repo_cleanup\
  p33_*\archive``); the constant survives so a record id can still be resolved when
  the archive is mounted at its original relative path.

The default can be pointed elsewhere with ``REAPER_MCP_EVIDENCE_DIR`` (exactly
that directory) or ``REAPER_MCP_RUN_ARTIFACTS`` (a root that still gets one
sub-directory per run); a *reader* of recorded runs points
``REAPER_MCP_EVIDENCE_SEARCH`` (or ``REAPER_MCP_RECOVERY_SEARCH`` for P8 recovery
points) at the archive. See `docs/universal-plugin-mcp-v3/12-testing-strategy.md` §11.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reaper_reapy_mcp.plugins import binding as B
from reaper_reapy_mcp.plugins.errors import InstanceNotFound, SnapshotUnavailable
from reaper_reapy_mcp.plugins.model import (
    FxHostFacts,
    PluginInstanceRef,
    StateRevision,
    TargetedStateSlice,
    text_digest,
)
from reaper_reapy_mcp.plugins.resolve import TargetToken, TargetTokenRegistry

__all__ = [
    "Target",
    "Baseline",
    "resolve_target",
    "capture_baseline",
    "restore_parameters",
    "compare_parameters",
    "changes_are_empty",
    "document_leaves_diff",
    "document_only_leaves",
    "route_table_leaves",
    "addresses_route_table",
    "describe_difference",
    "outside_span_is_intact",
    "control_in",
    "utc_now",
    "run_stamp",
    "write_run_json",
    "EVIDENCE_DIR",
    "RUN_DIR",
    "RECOVERY_DIR",
    "LEGACY_EVIDENCE_DIR",
    "LEGACY_RECOVERY_DIR",
    "user_data_root",
    "default_run_artifacts_root",
    "run_artifacts_root",
    "run_dirs",
    "run_task_id",
    "new_run_dir",
    "RunReport",
    "render_run_report",
    "write_run_report",
    "finalize_run",
    "provenance",
    "evidence_search_roots",
    "newest_evidence",
    "evidence_candidates",
    "recovery_search_roots",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DIR = Path(__file__).resolve().parent / "live"

#: The historical, repository-local evidence archive layout. Since P33 the corpus
#: lives OUTSIDE the repository (the external research archive); this constant is
#: where a reader looks if the archive was mounted at its original relative path.
LEGACY_EVIDENCE_DIR = LIVE_DIR / "evidence"
#: The recorded P8 recovery points. `restore_p8.py` restores FROM them; a *new*
#: recovery point is written to the run directory by `probe_p8_baseline.py`.
LEGACY_RECOVERY_DIR = LIVE_DIR / "recovery"
#: Kept as an alias: `RECOVERY_DIR` was the write path before P32.
RECOVERY_DIR = LEGACY_RECOVERY_DIR

RUN_ARTIFACTS_ENV = "REAPER_MCP_RUN_ARTIFACTS"
EVIDENCE_DIR_ENV = "REAPER_MCP_EVIDENCE_DIR"
RUN_TASK_ENV = "REAPER_MCP_RUN_TASK"
EVIDENCE_SEARCH_ENV = "REAPER_MCP_EVIDENCE_SEARCH"
RECOVERY_SEARCH_ENV = "REAPER_MCP_RECOVERY_SEARCH"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Run artifacts: generated evidence lives outside the repository by default
# ---------------------------------------------------------------------------
#
# Storage classes (P32):
#   A  tracked source, tests, fixtures, docs          -> in Git
#   B  runtime / prepared artifacts                   -> %LOCALAPPDATA%\reaper_reapy_mcp\
#                                                        {cache,discovery,calibration}
#   C  generated run evidence (JSON/WAV/PNG + report) -> %LOCALAPPDATA%\reaper_reapy_mcp\
#                                                        run_artifacts\<run>\
#   D  reproducible probes and live tests             -> in Git; their OUTPUT is C


def user_data_root(environ: Mapping[str, str] | None = None) -> Path:
    """The user-local base directory (same meaning as ``discovery/store.py``)."""
    env = os.environ if environ is None else environ
    local = env.get("LOCALAPPDATA") or env.get("XDG_CACHE_HOME")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def default_run_artifacts_root(environ: Mapping[str, str] | None = None) -> Path:
    """Where runs store their artifacts when no override is set (pure: no mkdir)."""
    return user_data_root(environ) / "reaper_reapy_mcp" / "run_artifacts"


def run_artifacts_root(environ: Mapping[str, str] | None = None) -> Path:
    """Artifact root: ``REAPER_MCP_RUN_ARTIFACTS`` if set, else the user-local default."""
    env = os.environ if environ is None else environ
    override = env.get(RUN_ARTIFACTS_ENV)
    if override and str(override).strip():
        return Path(override).expanduser()
    return default_run_artifacts_root(env)


def _slug(text: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in str(text)).strip("-_")


def run_task_id(environ: Mapping[str, str] | None = None, argv: Sequence[str] | None = None) -> str:
    """The run's task id: the probe/test script stem (override ``REAPER_MCP_RUN_TASK``)."""
    env = os.environ if environ is None else environ
    explicit = env.get(RUN_TASK_ENV)
    if explicit and str(explicit).strip():
        return _slug(explicit)
    args = list(sys.argv) if argv is None else list(argv)
    stem = Path(args[0]).stem if args and args[0] else "run"
    if "pytest" in stem:
        return "pytest-live-suite"
    return _slug(stem) or "run"


def new_run_dir(
    root: Path | None = None,
    *,
    task: str | None = None,
    when: datetime | None = None,
) -> Path:
    """A fresh, never-reused run directory: ``<UTC stamp>__<task>__<pid>`` (+``__2``...)."""
    base = Path(root) if root is not None else run_artifacts_root()
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}__{task or run_task_id()}__{os.getpid()}"
    candidate = base / name
    counter = 2
    while candidate.exists():
        candidate = base / f"{name}__{counter}"
        counter += 1
    return candidate


def _ensure_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"WARNING: cannot create the run-artifacts directory {path}: {exc}", file=sys.stderr)
        return False
    return True


_RUN_DIRS: tuple[Path, Path] | None = None


def run_dirs(*, refresh: bool = False) -> tuple[Path, Path]:
    """``(run_dir, evidence_dir)`` for THIS process - resolved once, then cached.

    ``REAPER_MCP_EVIDENCE_DIR``, when set, is used verbatim for both (no run
    sub-directory: the caller asked for exactly that path); otherwise a new run
    directory is made under the artifacts root.
    """
    global _RUN_DIRS
    if _RUN_DIRS is not None and not refresh:
        return _RUN_DIRS
    override = os.environ.get(EVIDENCE_DIR_ENV)
    if override and str(override).strip():
        base = Path(override).expanduser()
        _RUN_DIRS = (base, base)
    else:
        run = new_run_dir()
        _RUN_DIRS = (run, run / "evidence")
    _ensure_dir(_RUN_DIRS[0])
    _ensure_dir(_RUN_DIRS[1])
    return _RUN_DIRS


def __getattr__(name: str) -> Any:
    """PEP 562: ``EVIDENCE_DIR``/``RUN_DIR`` resolve lazily, so importing this
    module never creates a run directory - only using it (or writing) does."""
    if name in ("RUN_DIR", "EVIDENCE_DIR"):
        run, evidence = run_dirs()
        return run if name == "RUN_DIR" else evidence
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_run_state: dict[str, Any] = {"evidence": []}


def write_run_json(kind: str, payload: dict, directory: Path | None = None) -> Path | None:
    """Per-run evidence: unique name + content hash, never overwrites a run.

    Two runs of the same case produce two files, and the hash makes the artifact
    self-verifying: a re-run cannot silently replace a previous measurement.
    The default ``directory`` is THIS run's evidence directory (outside the
    repository); an explicit directory stays possible for a caller that must
    place a record somewhere chosen on purpose (a fixture-like input, an owner's
    archive). A failed save is reported and returns ``None`` - a measurement is
    never silently lost.
    """
    target = Path(directory) if directory is not None else run_dirs()[1]
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    path = target / f"{kind}__{run_stamp()}__{digest}.json"
    try:
        target.mkdir(parents=True, exist_ok=True)
        if path.exists():  # same second, same content: keep both runs distinguishable
            path = target / f"{kind}__{run_stamp()}__{digest}__2.json"
        # LF bytes: a digest taken over the text must describe the file on disk
        # (the default Windows text writer would translate \n into \r\n).
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"WARNING: could not save run evidence ({kind}) to {target}: {exc}", file=sys.stderr)
        return None
    _run_state["evidence"].append(path)
    return path


def _git_read(args: Sequence[str]) -> str | None:
    """A read-only git query; tolerant (no repo / no binary is not an error)."""
    if os.environ.get("REAPER_MCP_NO_GIT"):
        return None
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def provenance() -> dict[str, Any]:
    """What a later reader needs to place the run: commit, branch, interpreter."""
    info: dict[str, Any] = {
        "cwd": os.getcwd(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    if (REPO_ROOT / ".git").exists():
        info["commit"] = _git_read(["rev-parse", "--short", "HEAD"])
        info["branch"] = _git_read(["branch", "--show-current"])
    return info


@dataclass
class RunReport:
    """The short human-readable result every diagnostic run leaves behind."""

    task: str
    purpose: str = ""
    status: str = "unknown"  # ok | failed | partial | unknown
    started_utc: str | None = None
    duration_s: float | None = None
    tests: dict[str, Any] | None = None
    findings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    cost: dict[str, Any] | None = None


def _relative(target: Path, base: Path) -> str:
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return target.as_posix()


def render_run_report(
    report: RunReport,
    *,
    run_dir: Path,
    evidence_dir: Path,
    files: Sequence[Path],
    info: Mapping[str, Any],
) -> str:
    """Render the short Markdown result (pure: no filesystem access beyond `files`)."""
    lines: list[str] = [f"# Run `{run_dir.name}`", ""]
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append(f"| task | `{report.task}` |")
    if report.purpose:
        lines.append(f"| purpose | {report.purpose} |")
    lines.append(f"| status | **{report.status}** |")
    started = report.started_utc or "unknown"
    duration = f"{report.duration_s:.3f} s" if isinstance(report.duration_s, (int, float)) else "unknown"
    lines.append(f"| started / duration | {started} / {duration} |")
    lines.append(f"| commit | `{info.get('commit') or 'unknown'}` ({info.get('branch') or 'unknown'}) |")
    if report.tests:
        lines.append(
            "| tests | {passed} passed / {failed} failed / {skipped} skipped |".format(
                passed=report.tests.get("passed", "?"),
                failed=report.tests.get("failed", "?"),
                skipped=report.tests.get("skipped", "?"),
            )
        )
    if report.cost:
        cost = ", ".join(f"{key}={value}" for key, value in report.cost.items())
        lines.append(f"| cost | {cost} |")
    lines.append("")
    lines.append("## Findings")
    if report.findings:
        lines.extend(f"- {row}" for row in report.findings)
    else:
        lines.append("- (none recorded)")
    lines.append("")
    lines.append("## Evidence")
    if files:
        lines.append("| file | bytes |")
        lines.append("|---|---|")
        for path in files:
            link = _relative(path, run_dir)
            try:
                size = path.stat().st_size
            except OSError:
                size = "?"
            lines.append(f"| [{link}]({link}) | {size} |")
    else:
        lines.append("- (no artifact was recorded)")
    lines.append("")
    lines.append("## Limitations / not verified")
    if report.limitations:
        lines.extend(f"- {row}" for row in report.limitations)
    else:
        lines.append("- (none stated)")
    lines.append("")
    lines.append(f"Full artifacts: `{run_dir}`  ·  evidence directory: `{evidence_dir}`")
    lines.append("")
    lines.append("<!-- generated by tests/plugins/live_harness.py (P32 policy) -->")
    return "\n".join(lines) + "\n"


def write_run_report(
    report: RunReport,
    *,
    run_dir: Path | None = None,
    directory: Path | None = None,
) -> Path | None:
    """Write ``report.md`` + ``run.json`` beside the evidence (never in the repo)."""
    run = Path(run_dir) if run_dir is not None else run_dirs()[0]
    evidence = Path(directory) if directory is not None else run_dirs()[1]
    files = sorted(path for path in evidence.glob("*") if path.is_file()) if evidence.is_dir() else []
    info = provenance()
    text = render_run_report(report, run_dir=run, evidence_dir=evidence, files=files, info=info)
    manifest = {
        "run": run.name,
        "task": report.task,
        "purpose": report.purpose,
        "status": report.status,
        "started_utc": report.started_utc,
        "duration_s": report.duration_s,
        "tests": report.tests,
        "cost": report.cost,
        "findings": list(report.findings),
        "limitations": list(report.limitations),
        "provenance": info,
        "evidence": [_relative(path, evidence) for path in files],
        "report": "report.md",
    }
    if not _ensure_dir(run):
        return None
    try:
        (run / "report.md").write_text(text, encoding="utf-8", newline="\n")
        (run / "run.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        print(f"WARNING: could not write the run report to {run}: {exc}", file=sys.stderr)
        return None
    _run_state["report"] = run / "report.md"
    return _run_state["report"]


def finalize_run(
    kind: str,
    payload: dict,
    *,
    purpose: str = "",
    status: str | None = None,
    findings: Sequence[str] | None = None,
    limitations: Sequence[str] | None = None,
    tests: dict[str, Any] | None = None,
    duration_s: float | None = None,
    started_utc: str | None = None,
    directory: Path | None = None,
    run_dir: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Write the evidence JSON **and** the short report/manifest for this run.

    Status, cost and start time are derived from the payload when not given:
    ``exception``/``fatal`` means failed, ``totals`` becomes the cost row.
    """
    evidence_path = write_run_json(kind, payload, directory)
    if status is None:
        status = "failed" if (payload.get("exception") or payload.get("fatal")) else "ok"
    if started_utc is None:
        for key in ("started_utc", "utc", "generated_utc"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                started_utc = value
                break
    cost: dict[str, Any] | None = None
    totals = payload.get("totals")
    if isinstance(totals, Mapping):
        cost = {key: totals[key] for key in ("mcp_calls", "mcp_seconds", "wall_seconds", "response_bytes") if key in totals}
        if not cost:
            cost = None
    report = RunReport(
        task=run_task_id(),
        purpose=purpose,
        status=status,
        started_utc=started_utc,
        duration_s=duration_s,
        tests=tests,
        findings=list(findings or []),
        limitations=list(limitations or []),
        cost=cost,
    )
    report_path = write_run_report(report, run_dir=run_dir, directory=directory)
    return evidence_path, report_path


def evidence_search_roots() -> list[Path]:
    """Where a *reader* looks for recorded artifacts (archive layout, then run dirs).

    ``REAPER_MCP_EVIDENCE_SEARCH`` (``os.pathsep``-separated) replaces the search
    list when a caller must read recordings from an explicit location - the external
    research archive after P33. The archive is deliberately NOT a default root, so a
    test cannot depend on it silently.
    """
    override = os.environ.get(EVIDENCE_SEARCH_ENV)
    if override and str(override).strip():
        return [Path(part).expanduser() for part in str(override).split(os.pathsep) if part.strip()]
    roots = [LEGACY_EVIDENCE_DIR]
    runtime = run_artifacts_root()
    if runtime.is_dir():
        roots.append(runtime)
    return roots


def recovery_search_roots() -> list[Path]:
    """Where a reader looks for a P8 recovery point (explicit archive, then run dirs).

    ``REAPER_MCP_RECOVERY_SEARCH`` (``os.pathsep``-separated) replaces the default,
    which is the historical `recovery/` layout plus every run directory.
    """
    override = os.environ.get(RECOVERY_SEARCH_ENV)
    if override and str(override).strip():
        return [Path(part).expanduser() for part in str(override).split(os.pathsep) if part.strip()]
    roots = [LEGACY_RECOVERY_DIR]
    runtime = run_artifacts_root()
    if runtime.is_dir():
        roots.append(runtime)
    return roots


def newest_evidence(pattern: str, roots: Sequence[Path] | None = None) -> Path | None:
    """Newest recorded artifact matching ``pattern`` across the search roots.

    Deterministic (newest mtime wins, then path) and explicit: a reader of
    recorded runs names the pattern; it never guesses the newest *file in a
    directory* and never depends on a timestamp baked into a test.
    """
    candidates = evidence_candidates(pattern, roots)
    return candidates[0] if candidates else None


def evidence_candidates(pattern: str, roots: Sequence[Path] | None = None) -> list[Path]:
    """Every recorded artifact matching ``pattern``, newest first (then by path)."""
    found: list[Path] = []
    for root in roots if roots is not None else evidence_search_roots():
        if not Path(root).is_dir():
            continue
        found.extend(path for path in Path(root).glob(f"**/{pattern}") if path.is_file())
    return sorted(set(found), key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)


def _finalize_at_exit() -> None:
    """A run that wrote evidence but never finalized still leaves a short report.

    Deliberately quiet: an exit-time failure must never mask a measurement.
    """
    try:
        if _run_state.get("report") is not None or not _run_state.get("evidence"):
            return
        first = _run_state["evidence"][0]
        kind = first.name.split("__", 1)[0]
        write_run_report(
            RunReport(
                task=run_task_id(),
                purpose=f"(auto-summary for {kind}; the probe did not call finalize_run)",
                status="unknown",
                started_utc=utc_now(),
            )
        )
    except Exception:  # noqa: BLE001 - never raise at interpreter exit
        pass


atexit.register(_finalize_at_exit)


@dataclass
class Target:
    """One uniquely resolved live instance with its validated profile."""

    ref: PluginInstanceRef
    facts: FxHostFacts
    token: TargetToken
    registry: TargetTokenRegistry
    provider: Any
    profile: dict[str, Any]
    codec: Any


def resolve_target(bridge: Any, provider: Any, *, selector: dict[str, Any] | None = None) -> Target:
    """Exactly one matching instance, or a refusal that names the candidates."""
    facts_list: list[FxHostFacts] = []
    for track in bridge.list_tracks():
        for fx_index in range(int(track.n_fx)):
            facts = bridge.fx_facts(track.track_index, fx_index)
            if provider.matches(facts):
                facts_list.append(facts)
    if not facts_list:
        raise InstanceNotFound(
            "the harness found no instance the provider claims; it refuses to test",
            detail={"provider": str(provider.id), "selector": dict(selector or {})},
        )
    if len(facts_list) > 1:
        raise InstanceNotFound(
            "the harness resolved several instances and will not pick one; it refuses to test",
            detail={
                "provider": str(provider.id),
                "candidates": [
                    {"location": {"track_index": f.track_index, "fx_index": f.fx_index}, "fx_name": f.fx_name}
                    for f in facts_list
                ],
            },
        )
    facts = facts_list[0]
    registry = TargetTokenRegistry(session_context={"harness": "p8-live"})
    identity = provider.identify(facts)
    token = registry.issue(facts=facts, identity=identity)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": facts.track_index, "fx_index": facts.fx_index},
        provider=str(provider.id),
        identity=identity,
        display=f"{facts.fx_name} @ track {facts.track_index} fx {facts.fx_index}",
    )
    return Target(
        ref=ref,
        facts=facts,
        token=token,
        registry=registry,
        provider=provider,
        profile=provider.build_profile(identity),
        codec=provider.codec(),
    )


@dataclass
class Baseline:
    """The complete recovery set plus the measurements a report must cite."""

    chunk: str
    slice: TargetedStateSlice
    document: dict[str, Any]
    document_snapshot: Any
    parameters: dict[str, float]
    revision: StateRevision
    chunk_digest: str
    block_digest: str
    bound_profile: dict[str, Any] = field(default_factory=dict)
    binding_report: dict[str, Any] = field(default_factory=dict)
    derived_leaves: list[str] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)
    not_captured: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.not_captured


def capture_baseline(bridge: Any, target: Target, *, timeout_note: str = "") -> Baseline:
    """Capture the complete recovery set, or refuse before any write.

    ``not_captured`` names every channel that could not be captured; the caller
    must refuse when it is non-empty (a snapshot with a hole is not a recovery
    set). The identifier/name binding is measured here too, because a write that
    is not addressable is not a write this harness may attempt.
    """
    seconds: dict[str, float] = {}
    not_captured: list[str] = []

    started = time.perf_counter()
    chunk = bridge.read_state_chunk(target.ref)
    facts = bridge.fx_facts(target.ref.location["track_index"], target.ref.location["fx_index"])
    slice_ = bridge.locate_state_slice(target.ref, facts, chunk)
    seconds["state_read"] = round(time.perf_counter() - started, 4)
    if not slice_.is_valid_for(chunk):
        not_captured.append("document")

    started = time.perf_counter()
    document = target.codec.decode(slice_)
    document_snapshot = target.codec.snapshot_document(document)
    seconds["document_decode"] = round(time.perf_counter() - started, 4)
    if document_snapshot is None:
        not_captured.append("document_snapshot")

    count = int(facts.param_count or 0)
    started = time.perf_counter()
    idents = bridge.param_idents(target.ref, list(range(count))) if count else []
    seconds["parameter_idents"] = round(time.perf_counter() - started, 4)
    started = time.perf_counter()
    values = bridge.read_params(target.ref, list(range(count))) if count else []
    seconds["parameter_values"] = round(time.perf_counter() - started, 4)
    if not count or len(idents) != count or len(values) != count or any(i is None for i in idents):
        not_captured.append("parameters")
    parameters = {str(i): float(v) for i, v in zip(idents, values) if i}

    started = time.perf_counter()
    indexed = B.indexed_controls(target.profile)
    names = bridge.param_names(target.ref, [index for _cid, index in indexed]) if indexed else []
    ident_at = bridge.param_idents(target.ref, [index for _cid, index in indexed]) if indexed else []
    seconds["binding"] = round(time.perf_counter() - started, 4)
    _profile_copy, binding_report = B.bind_profile(
        target.profile, indexed=indexed, names=names, idents=ident_at
    )

    revision = StateRevision.compute(state_channel=slice_.block_text, parameters=parameters)
    return Baseline(
        chunk=chunk,
        slice=slice_,
        document=document,
        document_snapshot=document_snapshot,
        parameters=parameters,
        revision=revision,
        chunk_digest=text_digest(chunk),
        block_digest=slice_.block_digest,
        bound_profile=_profile_copy,
        binding_report=binding_report.as_dict(),
        derived_leaves=[
            str(path) for path in ((target.profile.get("state") or {}).get("derived_leaves") or [])
        ],
        seconds=seconds,
        not_captured=not_captured,
    )


def restore_parameters(
    bridge: Any, ref: PluginInstanceRef, captured: dict[str, float], *, batch: int = 256
) -> dict[str, Any]:
    """Write every captured value back through the transport (the engine's own
    restore mechanism), then read it back and compare per ident.

    The read-back resolves each **ident** to its current index through the live
    ident table - the same durable binding the write used. Reading by position
    instead would report a restore of whatever happens to sit at index 0..N and
    would call a genuine restore a failure (that mistake was made and caught
    here: the comparison named the ident it could not find).
    """
    pairs = list(captured.items())
    attempts: list[Any] = []
    for start in range(0, len(pairs), batch):
        attempts.append(bridge.write_params(ref, pairs[start : start + batch]))

    count = int(bridge.fx_facts(ref.location["track_index"], ref.location["fx_index"]).param_count or 0)
    idents = bridge.param_idents(ref, list(range(count)))
    values = bridge.read_params(ref, list(range(count)))
    observed_by_ident = {str(i): float(v) for i, v in zip(idents, values) if i}
    readback = {ident: observed_by_ident.get(ident) for ident in captured}
    comparison = compare_parameters(
        captured, {k: v for k, v in readback.items() if v is not None}, tolerance=0.0
    )
    comparison["idents_read"] = len(observed_by_ident)
    comparison["ident_not_in_table"] = sorted(k for k, v in readback.items() if v is None)
    comparison["all_equal"] = bool(
        comparison["all_equal"] and not comparison["ident_not_in_table"]
    )
    return {
        "write_calls": len(attempts),
        "pairs": len(pairs),
        "comparison": comparison,
        "restored": comparison["all_equal"],
    }


def compare_parameters(
    expected: dict[str, float], observed: dict[str, float], *, tolerance: float = 0.0
) -> dict[str, Any]:
    """Per-ident comparison; a missing ident is a difference, not a zero."""
    mismatched: list[dict[str, Any]] = []
    for ident, value in expected.items():
        actual = observed.get(ident)
        if actual is None:
            mismatched.append({"ident": ident, "expected": value, "observed": None})
        elif abs(float(actual) - float(value)) > tolerance:
            mismatched.append({"ident": ident, "expected": value, "observed": float(actual)})
    extra = [ident for ident in observed if ident not in expected]
    return {
        "compared": len(expected),
        "missing": sum(1 for row in mismatched if row["observed"] is None),
        "mismatched": mismatched[:20],
        "n_mismatched": len(mismatched),
        "extra_idents_present": len(extra),
        "tolerance": tolerance,
        "all_equal": not mismatched and len(expected) == len(observed),
    }


def derived_leaf_gaps(declared_derived: Sequence[str]) -> list[dict[str, Any]]:
    """The canonical named gap for every declared derived leaf.

    One representation for one fact (Decision 2/5, 2026-10-10):

    - ``derived_excluded`` (a codec change-set/restore field) is the
      **mechanism** - which captured leaves the restore set leaves out;
    - ``derived_leaf_not_restored`` (an outcome ``named_gaps`` entry, and the
      same row here) is the **consequence** - one row per excluded leaf, present
      whether the operation failed, only partly restored, or restored everything
      else successfully.
    """
    return [
        {
            "channel": "state",
            "reason": "derived_leaf_not_restored",
            "path": str(path),
            "detail": (
                "the provider declares this leaf as plugin-derived; a document write "
                "cannot set it"
            ),
        }
        for path in sorted({str(item) for item in declared_derived if str(item)})
    ]


def restore_completeness(
    *, channels_verified: bool, declared_derived: Sequence[str] = ()
) -> dict[str, Any]:
    """`verified` (what was written back landed) kept apart from `complete`.

    A restore that deliberately excluded a plugin-derived leaf is verified and
    **not** complete; reporting the pair is what keeps "the remaining channels are
    back" distinguishable from "everything is back".
    """
    excluded = [str(item) for item in declared_derived if str(item)]
    return {
        "verified": bool(channels_verified),
        "complete": bool(channels_verified) and not excluded,
        "excluded": sorted(excluded),
        "named_gaps": derived_leaf_gaps(excluded),
    }


def describe_difference(captured: Any, observed: Any) -> dict[str, Any]:
    """A size-and-position description of two differing values (for evidence).

    Records *where* two strings differ rather than only that they do: a
    difference confined to a prefix or a suffix is a different finding from one
    spread through the payload, and that distinction decided a live case here.
    When both values are base64 text, their decoded byte pictures are compared
    too (lengths, leading zero bytes, byte-level equality), because a text
    difference can be a pure re-encoding of identical bytes.
    """
    if not (isinstance(captured, str) and isinstance(observed, str)):
        return {"type": type(observed).__name__, "captured": captured, "observed": observed}
    limit = min(len(captured), len(observed))
    first = next((i for i in range(limit) if captured[i] != observed[i]), None)
    last = next(
        (limit - 1 - i for i in range(limit) if captured[limit - 1 - i] != observed[limit - 1 - i]),
        None,
    )
    description: dict[str, Any] = {
        "type": "string",
        "captured_chars": len(captured),
        "observed_chars": len(observed),
        "first_difference_index": first,
        "last_difference_index": last,
    }
    captured_bytes = _try_b64(captured)
    observed_bytes = _try_b64(observed)
    if captured_bytes is not None and observed_bytes is not None:
        description["base64"] = {
            "captured_bytes": len(captured_bytes),
            "observed_bytes": len(observed_bytes),
            "identical": captured_bytes == observed_bytes,
            "captured_leading_zero_bytes": len(captured_bytes)
            - len(captured_bytes.lstrip(b"\x00")),
            "observed_leading_zero_bytes": len(observed_bytes)
            - len(observed_bytes.lstrip(b"\x00")),
        }
    return description


def _try_b64(text: str) -> bytes | None:
    import base64

    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except Exception:  # noqa: BLE001 - "not base64" is a legitimate answer here
        return None


def changes_are_empty(changes: Any) -> bool:
    """True when a codec change set asks for nothing (a comparison, not an assumption)."""
    if not isinstance(changes, dict):
        return not changes
    for key in ("paths", "routes_create", "routes_update", "routes_remove"):
        if changes.get(key):
            return False
    return True


def document_leaves_diff(
    document_now: dict, snapshot: Any, *, declared_derived: Sequence[str] = ()
) -> dict[str, Any]:
    """Compare every **captured** settings leaf against the live document.

    This is the document channel's verification, and it is deliberately a
    comparison rather than a change-set request: `VitalCodec.restore_changes`
    returns *every* captured leaf (its restore is "write them all back", which is
    idempotent), so asking it "what still differs?" would report the leaf count
    for any state - a number that looks like a measurement and is not.

    Leaves the provider declares **plugin-derived** are reported separately: the
    plugin re-serializes them from its own buffer, so a difference there is a
    declared property of the host, not a failed restore - and it must still be
    visible in the report (that is the whole point of naming them).
    """
    derived = {str(path) for path in declared_derived}
    captured = _settings_leaves(snapshot)
    live = _settings_leaves(document_now)
    mismatched: list[dict[str, Any]] = []
    derived_rows: list[dict[str, Any]] = []
    derived_compared = 0
    for path, value in captured.items():
        actual = live.get(path)
        differs = (
            abs(float(actual) - float(value)) > 1e-6
            if isinstance(value, float) and isinstance(actual, (int, float))
            else actual != value
        )
        if path in derived:
            derived_compared += 1
            if differs:
                derived_rows.append({"path": path, "captured": value, "observed": actual})
            continue
        if differs:
            mismatched.append({"path": path, "captured": value, "observed": actual})
    missing = [path for path in live if path not in captured]
    return {
        "compared": len(captured) - derived_compared,
        "n_mismatched": len(mismatched),
        "mismatched": mismatched[:20],
        "new_paths_not_captured": len(missing),
        "new_paths_sample": missing[:20],
        "declared_derived": sorted(derived),
        "derived_compared": derived_compared,
        "n_derived_mismatched": len(derived_rows),
        "derived_mismatched": derived_rows[:5],
        "all_equal": not mismatched,
    }


def _settings_leaves(document_or_snapshot: Any) -> dict[str, Any]:
    """The settings subtree as dotted paths, from a decoded view or a snapshot."""
    from reaper_reapy_mcp.plugins.providers.vital import codec as VC

    if isinstance(document_or_snapshot, dict) and "document" in document_or_snapshot:
        source = document_or_snapshot
    else:
        source = {"document": document_or_snapshot}
    settings = (source["document"] or {}).get("settings") or {}
    return VC.flat_paths({"settings": settings})


def addresses_route_table(path: str) -> bool:
    """Does this captured/planned path address the codec's route table?

    The harness re-exports the provider's own predicate (which is itself derived
    from the codec's constants) so a live case can assert the reason it skipped a
    candidate without importing a second, hand-typed notion of "the route table".
    """
    from reaper_reapy_mcp.plugins.providers.vital import dependencies_rules as RULES

    return bool(RULES.addresses_route_table(path))


def route_table_leaves(document_or_snapshot: Any) -> list[str]:
    """Captured settings leaves that address the codec's route table.

    Named separately because these are **not** usable as a live state-write case
    any more: the dependency gate refuses any state write under the route table
    that does not clear a pair field (owner safety-gate correction, 2026-10-11).
    A harness that offered one would drive a live case straight into a refusal and
    report it as a write failure. The predicate is the codec's own
    (`RULES.addresses_route_table`), so the two cannot drift.
    """
    from reaper_reapy_mcp.plugins.providers.vital import dependencies_rules as RULES

    return sorted(
        path for path in _settings_leaves(document_or_snapshot) if RULES.addresses_route_table(path)
    )


def document_only_leaves(
    profile: dict[str, Any], document_or_snapshot: Any
) -> dict[str, Any]:
    """Captured settings leaves that are **not** bound to a declared control.

    A live state-write case needs a leaf the plugin stores in its document without
    also exposing it as a parameter (a parameter-mirroring key is a different
    case). The set is derived from the profile's own bindings and the live
    document - never from a hard-coded path - and the caller skips when it is
    empty rather than inventing one.

    Leaves under the codec's route table are excluded: a write to them is refused
    by the dependency gate, so offering one would make the live case fail for a
    reason that has nothing to do with the write path it is testing. They are
    still enumerated by `route_table_leaves` for the record.
    """
    from reaper_reapy_mcp.plugins.providers.vital import dependencies_rules as RULES

    bound = {
        str((row.get("binding") or {}).get("state_key"))
        for row in profile.get("controls") or []
        if (row.get("binding") or {}).get("state_key")
    }
    leaves = _settings_leaves(document_or_snapshot)
    out: dict[str, Any] = {}
    for path, value in leaves.items():
        key = path.split(".", 1)[1] if path.startswith("settings.") else path
        if key in bound:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        if RULES.addresses_route_table(path):
            continue
        out[path] = value
    return out


def control_in(profile: dict[str, Any], control_id: str) -> dict[str, Any]:
    """The declared control, or a refusal naming what the profile does offer."""
    for row in profile.get("controls") or []:
        if str(row.get("id")) == control_id:
            return row
    raise SnapshotUnavailable(
        "the profile does not declare the control the harness needs; it refuses to test",
        detail={"control": control_id, "declared": len(profile.get("controls") or [])},
    )


def outside_span_is_intact(
    before: str, after: str, slice_before: TargetedStateSlice, slice_after: TargetedStateSlice | None = None
) -> dict[str, Any]:
    """Every byte outside the located span is identical before/after a write.

    The span is located **in each chunk** (a document write may change the span's
    own length, so comparing by the old line indices would compare the wrong
    lines and report a false failure - that mistake was made on the first live
    run). Everything before the span start and after the span end must be equal.
    """
    after_slice = slice_after or slice_before
    before_lines = before.split("\n")
    after_lines = after.split("\n")
    prefix_before = "\n".join(before_lines[: slice_before.line_start])
    prefix_after = "\n".join(after_lines[: after_slice.line_start])
    suffix_before = "\n".join(before_lines[slice_before.line_end + 1 :])
    suffix_after = "\n".join(after_lines[after_slice.line_end + 1 :])
    return {
        "prefix_identical": prefix_before == prefix_after,
        "suffix_identical": suffix_before == suffix_after,
        "span_lines_before": slice_before.line_end - slice_before.line_start + 1,
        "span_lines_after": after_slice.line_end - after_slice.line_start + 1,
        "line_count_before": len(before_lines),
        "line_count_after": len(after_lines),
        "chunk_chars_before": len(before),
        "chunk_chars_after": len(after),
        "intact": bool(prefix_before == prefix_after and suffix_before == suffix_after),
    }
