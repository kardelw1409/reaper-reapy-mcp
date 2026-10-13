"""P32/P33 — the evidence policy as a *mechanical* property (offline).

The rounds that created this file changed where a live run's results are stored:
since P32 the one writer (`tests/plugins/live_harness.py`) defaults to a directory
**outside the repository**, every run leaves a short Markdown report, and the
regression suite reads a deterministic fixture instead of the newest file in the
recorded-run archive. P33 removed the historical corpus itself and moved it to the
external research archive.

These tests pin the properties that would otherwise decay silently:

1. the default artifacts root is outside the repository (pure function, no mkdir);
2. `REAPER_MCP_EVIDENCE_DIR` is used verbatim, `REAPER_MCP_RUN_ARTIFACTS` still gets
   one directory per run, and two runs never share a directory;
3. `write_run_json` writes to the run's evidence directory by default, with LF bytes
   (a digest taken over the text must describe the file on disk);
4. `finalize_run`/`write_run_report` produce `report.md` + `run.json` whose evidence
   links are **relative** and whose fields name the run, status, cost and limits;
5. a save that cannot happen is reported and returns `None` - never raised (a
   finished measurement is not lost to an exception);
6. a run that only wrote evidence still gets a summary report at exit;
7. **no module under `tests/plugins` re-introduces a repository-local evidence
   directory** by assignment (the source-level guard that keeps the next probe from
   writing into the tree again);
8. the P19 read-leg test reads the committed fixture and no longer names the
   archive at all, and the fixture carries its provenance.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

import live_harness as H

HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolated_run_state(monkeypatch):
    """No test may leak a run-directory resolution or a written-file record."""
    saved_dirs = H._RUN_DIRS
    saved_state = {
        key: (list(value) if isinstance(value, list) else value) for key, value in H._run_state.items()
    }
    H._run_state.clear()
    H._run_state.update({"evidence": []})
    monkeypatch.setenv("REAPER_MCP_NO_GIT", "1")  # provenance must not need a repo
    yield
    H._RUN_DIRS = saved_dirs
    H._run_state.clear()
    H._run_state.update(saved_state)

# ---------------------------------------------------------------------------
# 1-2. resolution
# ---------------------------------------------------------------------------


def test_the_default_artifacts_root_is_outside_the_repository():
    root = H.default_run_artifacts_root()
    repository = Path(__file__).resolve().parents[2]
    assert not root.is_relative_to(repository), root
    assert root.name == "run_artifacts"
    assert root.parent.name == "reaper_reapy_mcp"


def test_the_artifacts_root_override_changes_the_root_only(monkeypatch, tmp_path):
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "root"))
    monkeypatch.setenv("REAPER_MCP_RUN_TASK", "p32-policy")
    run, evidence = H.run_dirs(refresh=True)
    assert run.parent == tmp_path / "root"
    assert run.name.startswith("2") and "__p32-policy__" in run.name
    assert evidence == run / "evidence" and evidence.is_dir()


def test_each_run_gets_its_own_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "root"))
    first, _ = H.run_dirs(refresh=True)
    second, _ = H.run_dirs(refresh=True)
    assert first != second
    assert first.is_dir() and second.is_dir()


def test_the_evidence_directory_override_is_used_verbatim(monkeypatch, tmp_path):
    explicit = tmp_path / "exactly-here"
    monkeypatch.setenv("REAPER_MCP_EVIDENCE_DIR", str(explicit))
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "ignored"))
    run, evidence = H.run_dirs(refresh=True)
    assert (run, evidence) == (explicit, explicit)
    assert explicit.is_dir()
    assert not (tmp_path / "ignored").exists(), "the verbatim override must not create a run dir"


# ---------------------------------------------------------------------------
# 3. writing
# ---------------------------------------------------------------------------


def test_write_run_json_defaults_to_the_run_evidence_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "root"))
    written = H.write_run_json("p32_policy_default", {"b": 2, "a": 1})
    assert written is not None
    run, evidence = H.run_dirs()
    assert written.parent == evidence
    assert written.name.startswith("p32_policy_default__")
    raw = written.read_bytes()
    assert b"\r\n" not in raw, "a digest over the text must describe the bytes on disk"
    digest = hashlib.sha256(
        json.dumps({"b": 2, "a": 1}, indent=2, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    assert digest in written.name


def test_writing_twice_never_overwrites_a_run(monkeypatch, tmp_path):
    explicit = tmp_path / "ev"
    monkeypatch.setenv("REAPER_MCP_EVIDENCE_DIR", str(explicit))
    H.run_dirs(refresh=True)
    first = H.write_run_json("p32_policy_same", {"x": 1})
    second = H.write_run_json("p32_policy_same", {"x": 1})
    assert first is not None and second is not None
    assert first != second and first.exists() and second.exists()


def test_a_save_that_cannot_happen_is_reported_not_raised(monkeypatch, tmp_path, capsys):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    result = H.write_run_json("p32_policy_fail", {"x": 1}, blocker / "sub")
    assert result is None
    assert "could not save run evidence" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 4-6. the short report
# ---------------------------------------------------------------------------


def test_the_report_names_the_run_and_links_evidence_relatively(monkeypatch, tmp_path):
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "root"))
    monkeypatch.setenv("REAPER_MCP_RUN_TASK", "p32-report")
    H.run_dirs(refresh=True)
    evidence_path, report_path = H.finalize_run(
        "p32_policy_report",
        {
            "started_utc": "2026-10-13T00:00:00Z",
            "totals": {"mcp_calls": 3, "mcp_seconds": 1.5, "response_bytes": 99},
        },
        purpose="the report shape",
        findings=["one finding"],
        limitations=["one limitation"],
        tests={"passed": 5, "failed": 1, "skipped": 2},
    )
    assert evidence_path is not None and report_path is not None
    run_dir, evidence_dir = H.run_dirs()
    assert report_path == run_dir / "report.md"
    text = report_path.read_text(encoding="utf-8")
    assert f"# Run `{run_dir.name}`" in text
    assert "| status | **ok** |" in text
    assert "| tests | 5 passed / 1 failed / 2 skipped |" in text
    assert "mcp_calls=3" in text and "response_bytes=99" in text
    assert "- one finding" in text and "- one limitation" in text
    link = f"evidence/{evidence_path.name}"
    assert f"[{link}]({link})" in text
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["task"] == "p32-report"
    assert manifest["status"] == "ok" and manifest["report"] == "report.md"
    assert manifest["evidence"] == [evidence_path.name]
    assert manifest["provenance"]["python"]
    assert not Path(manifest["evidence"][0]).is_absolute()


def test_a_failure_in_the_payload_makes_the_report_say_so(monkeypatch, tmp_path):
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "root"))
    H.run_dirs(refresh=True)
    _evidence, report_path = H.finalize_run("p32_policy_failed", {"fatal": {"at": "step-2"}})
    assert report_path is not None
    assert "| status | **failed** |" in report_path.read_text(encoding="utf-8")


def test_a_run_without_finalize_still_leaves_a_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("REAPER_MCP_RUN_ARTIFACTS", str(tmp_path / "root"))
    H.run_dirs(refresh=True)
    assert H.write_run_json("p32_policy_autosummary", {"x": 1}) is not None
    assert H._run_state.get("report") is None, "nothing may write a report before the run ends"
    H._finalize_at_exit()
    report = H.run_dirs()[0] / "report.md"
    assert report.is_file(), "an un-finalized run must still leave a short report"
    text = report.read_text(encoding="utf-8")
    assert "auto-summary" in text and "p32_policy_autosummary" in text
    # a second call must be a no-op, not a rewrite
    before = report.read_bytes()
    H._finalize_at_exit()
    assert report.read_bytes() == before


# ---------------------------------------------------------------------------
# 7. the source-level guard
# ---------------------------------------------------------------------------


def _evidence_assignments(source: str, name: str) -> list[str]:
    """Assignments of EVIDENCE/EVIDENCE_DIR (or <x>evidence_dir) to a *literal path*."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id.lower() not in ("evidence", "evidence_dir"):
                continue
            literal = ast.unparse(value) if hasattr(ast, "unparse") else ""
            if '"evidence"' in literal and "LEGACY" not in literal:
                hits.append(f"{name}:{node.lineno}: {target.id} = {literal}")
    return hits


def test_no_module_reintroduces_a_repository_local_evidence_directory():
    """The one allowed definition is the harness's own legacy-archive constant.

    A probe or test that wants the *archive* uses ``LEGACY_EVIDENCE_DIR`` from the
    harness; anything else that assigns a literal ``"evidence"`` directory is a new
    write target inside the tree and must fail this guard (P32 §5/§7).
    """
    offenders: list[str] = []
    for path in sorted(HERE.rglob("*.py")):
        if path.name == "live_harness.py":
            continue
        offenders.extend(_evidence_assignments(path.read_text(encoding="utf-8"), path.name))
    assert offenders == [], "repository-local evidence directories were reintroduced:\n" + "\n".join(offenders)


def test_the_harness_archive_constant_points_at_the_recorded_runs():
    text = (HERE / "live_harness.py").read_text(encoding="utf-8")
    assert 'LEGACY_EVIDENCE_DIR = LIVE_DIR / "evidence"' in text
    assert 'LEGACY_RECOVERY_DIR = LIVE_DIR / "recovery"' in text


# ---------------------------------------------------------------------------
# 8. the decoupled test and its fixture
# ---------------------------------------------------------------------------


def test_the_read_leg_test_no_longer_reads_the_recorded_archive():
    source = (HERE / "test_p19_enum_positions.py").read_text(encoding="utf-8")
    assert 'live" / "evidence' not in source
    assert "live/evidence" not in source
    assert "candidates[-1]" not in source, "the newest-file dependency must be gone"
    assert 'vital_host_bands' in source


def test_the_host_band_map_fixture_carries_its_provenance():
    fixture = HERE / "fixtures" / "vital_host_bands" / "filter_1_style_grid.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    assert document["control"] == "filter_1_style"
    assert document["value_kind"] == "normalized"
    recorded = document["recorded_by"]
    assert recorded["grids_agree"] is True
    assert len(recorded["files"]) >= 3
    for row in recorded["files"]:
        assert len(row["sha256"]) == 64, row
        assert row["file"].startswith("p19_enum_bands_through_host__")
        assert row["points"] == document["counts"]["points"]
    counts = document["counts"]
    assert counts["points"] == len(document["grid"])
    assert counts["labelled"] + counts["unlabelled"] == counts["points"]
    assert counts["labelled"] >= 30 and counts["unlabelled"] >= 1


def test_the_fixture_builder_reproduces_the_committed_fixture_without_the_archive(tmp_path):
    """The committed fixture is exactly what the builder's pure functions produce.

    Since P33 the original recordings are NOT in the repository (they live in the
    external research archive), so this test must not need them: the grid and the
    provenance rows are read from the committed fixture itself, run through
    `assemble` + `serialize`, and compared byte for byte. A serializer that drifts
    (or a hand-edited fixture) fails here; nothing in the repository is written.
    """
    import importlib.util

    builder_path = HERE / "fixtures" / "build_vital_host_bands_fixture.py"
    spec = importlib.util.spec_from_file_location("build_vital_host_bands_fixture", builder_path)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    fixture = HERE / "fixtures" / "vital_host_bands" / "filter_1_style_grid.json"
    committed = fixture.read_bytes()
    document = json.loads(committed.decode("utf-8"))
    grid = [[point[0], point[1]] for point in document["grid"]]
    rebuilt = builder.serialize(builder.assemble([grid], document["recorded_by"]["files"]))
    assert rebuilt.encode("utf-8") == committed, "the builder no longer reproduces the committed fixture"
    assert fixture.read_bytes() == committed


def test_the_fixture_builder_refuses_when_recordings_disagree():
    """An extraction must never pick one recording silently (P33 6.3)."""
    import importlib.util

    builder_path = HERE / "fixtures" / "build_vital_host_bands_fixture.py"
    spec = importlib.util.spec_from_file_location("build_vital_host_bands_fixture_refusal", builder_path)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    first = [{"normalized": 0.0, "display": "12dB"}]
    second = [{"normalized": 0.0, "display": "24dB"}]
    agree = builder.extract_grids([(Path("a.json"), first), (Path("b.json"), first)])
    assert agree == [[[0.0, "12dB"]], [[0.0, "12dB"]]], agree
    with pytest.raises(builder.ExtractionRefused):
        builder.extract_grids([(Path("a.json"), first), (Path("b.json"), second)])
    with pytest.raises(builder.ExtractionRefused):
        builder.extract_grids([])


def test_the_fixture_builder_cli_says_so_when_there_is_no_recording(tmp_path):
    """With no archive configured the CLI reports it and writes nothing (exit 2)."""
    import subprocess
    import sys

    empty = tmp_path / "empty"
    empty.mkdir()
    target = tmp_path / "out.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(HERE / "fixtures" / "build_vital_host_bands_fixture.py"),
            "--search",
            str(empty),
            "--out",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(HERE.parents[1]),
        env={**os.environ, "REAPER_MCP_EVIDENCE_SEARCH": str(empty)},
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "no recording" in completed.stdout
    assert not target.exists()
