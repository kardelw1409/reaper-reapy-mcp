"""Where the images go: the repository's existing run-artifact scheme.

`AGENTS.md` §2 forbids inventing a second storage scheme for generated run
artifacts, so the vision layer applies the same rules as
`tests/plugins/live_harness.py`. That agreement is asserted here against the
harness ITSELF (imported from its file), not against a copy of its constants: a
change to the harness root or to an environment variable name would otherwise
split the two silently, and the images would start landing in a directory nobody
sweeps.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reaper_reapy_mcp.vision import artifacts

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_harness():
    """Import tests/plugins/live_harness.py by path (it is not a package).

    The module has to be registered in `sys.modules` BEFORE it runs: it uses
    `from __future__ import annotations`, and dataclasses resolves those string
    annotations through `sys.modules`, so an unregistered module fails at import
    with a NameError inside `_is_type` (measured here).
    """
    path = REPO_ROOT / "tests" / "plugins" / "live_harness.py"
    name = "vision_live_harness_probe"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_root_is_the_harness_root_when_nothing_overrides_it():
    harness = _load_harness()
    assert artifacts.default_run_artifacts_root() == harness.default_run_artifacts_root()
    assert artifacts.user_data_root() == harness.user_data_root()
    assert artifacts.RUN_ARTIFACTS_ENV == harness.RUN_ARTIFACTS_ENV
    assert artifacts.EVIDENCE_DIR_ENV == harness.EVIDENCE_DIR_ENV
    assert artifacts.RUN_TASK_ENV == harness.RUN_TASK_ENV


@pytest.mark.parametrize("environ", [
    {},
    {"LOCALAPPDATA": r"C:\Users\somebody\AppData\Local"},
    {"XDG_CACHE_HOME": "/home/somebody/.cache"},
    {"LOCALAPPDATA": r"C:\somewhere", artifacts.RUN_ARTIFACTS_ENV: r"D:\runs"},
])
def test_the_root_follows_the_same_environment_rules_as_the_harness(environ):
    harness = _load_harness()
    assert artifacts.user_data_root(environ) == harness.user_data_root(environ)
    assert (artifacts.run_artifacts_root(environ)
            == harness.run_artifacts_root(environ)), environ


def test_the_run_directory_follows_the_harness_naming(tmp_path):
    when = datetime(2026, 10, 18, 17, 30, tzinfo=timezone.utc)
    directory = artifacts.new_run_dir("vision", when=when, pid=4242,
                                      root=tmp_path, environ={})
    assert directory.name == "20261018T173000Z__vision__4242"
    assert directory.is_dir()


def test_a_run_directory_is_never_reused(tmp_path):
    when = datetime(2026, 10, 18, 17, 30, tzinfo=timezone.utc)
    first = artifacts.new_run_dir("vision", when=when, pid=1, root=tmp_path, environ={})
    second = artifacts.new_run_dir("vision", when=when, pid=1, root=tmp_path, environ={})
    assert first != second
    assert second.name.endswith("__2")


def test_the_task_id_can_be_named_by_the_environment(tmp_path):
    when = datetime(2026, 10, 18, 17, 30, tzinfo=timezone.utc)
    directory = artifacts.new_run_dir(when=when, pid=7, root=tmp_path,
                                      environ={artifacts.RUN_TASK_ENV: "p44"})
    assert directory.name.endswith("__p44__7")


def test_an_explicit_vision_dir_wins_over_the_shared_root(tmp_path, monkeypatch):
    target = tmp_path / "my shots"
    monkeypatch.setenv(artifacts.VISION_DIR_ENV, str(target))
    monkeypatch.setenv(artifacts.RUN_ARTIFACTS_ENV, str(tmp_path / "runs"))
    artifacts.reset_session_dir()
    try:
        assert artifacts.shots_dir() == target
        assert target.is_dir()
    finally:
        artifacts.reset_session_dir()


def test_the_evidence_dir_of_a_run_is_used_verbatim(tmp_path, monkeypatch):
    """Same rule the harness applies: the override points AT the directory."""
    evidence = tmp_path / "evidence"
    monkeypatch.delenv(artifacts.VISION_DIR_ENV, raising=False)
    monkeypatch.setenv(artifacts.EVIDENCE_DIR_ENV, str(evidence))
    artifacts.reset_session_dir()
    try:
        assert artifacts.shots_dir() == evidence
        assert not list(evidence.glob("*"))       # used verbatim, no run dir inside
    finally:
        artifacts.reset_session_dir()


def test_unique_path_never_returns_a_name_that_exists(tmp_path):
    target = tmp_path / "shot.png"
    assert artifacts.unique_path(target) == target
    target.write_bytes(b"x")
    second = artifacts.unique_path(target)
    assert second.name == "shot__2.png"
    second.write_bytes(b"x")
    assert artifacts.unique_path(target).name == "shot__3.png"


def test_a_written_shot_gets_a_png_and_a_labelled_sidecar(tmp_path):
    from vision_support import make_shot

    shot = make_shot(64, 48)
    meta = artifacts.write_shot(shot, {"view": [1.0, 2.0]}, label="My Shot/1",
                                directory=tmp_path)
    assert Path(meta["png"]).is_file()
    assert Path(meta["png"]).name.endswith("__My_Shot_1.png"), meta["png"]
    assert Path(meta["meta_json"]).is_file()
    assert Path(meta["meta_json"]).name.startswith(Path(meta["png"]).stem)
    assert meta["png_bytes"] > 0
    # the label is sanitised, so a caller cannot escape the directory
    assert "/" not in Path(meta["png"]).name
