"""Where a vision run writes its images - the SAME scheme the repo already uses.

`AGENTS.md` §2 forbids a second storage scheme for generated run artifacts:
runs write under ``%LOCALAPPDATA%\\reaper_reapy_mcp\\run_artifacts``, one
directory per run (``<UTC stamp>__<task>__<pid>``), and the roots are overridable
with ``REAPER_MCP_RUN_ARTIFACTS`` / ``REAPER_MCP_EVIDENCE_DIR``. This module
applies exactly those rules, so a screenshot lands beside the evidence of the
run that asked for it instead of inventing a directory of its own.

Differences from the test harness, and why they are not a new scheme:

* the "run" is the SERVER SESSION, so the directory is created once per process
  and reused for every shot of that session (a per-call directory would bury a
  scan in twenty folders);
* one optional product-level override, ``REAPER_VISION_DIR``, points the images
  somewhere explicit. When it is unset - the default - the root is the shared
  run-artifact root, byte for byte.

`tests/vision/test_vision_artifacts.py` pins the agreement with
``tests/plugins/live_harness.py`` so the two cannot drift apart.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "RUN_ARTIFACTS_ENV",
    "EVIDENCE_DIR_ENV",
    "RUN_TASK_ENV",
    "VISION_DIR_ENV",
    "SESSION_TASK",
    "user_data_root",
    "default_run_artifacts_root",
    "run_artifacts_root",
    "new_run_dir",
    "shots_dir",
    "reset_session_dir",
    "write_shot",
    "write_json",
    "unique_path",
]

RUN_ARTIFACTS_ENV = "REAPER_MCP_RUN_ARTIFACTS"
EVIDENCE_DIR_ENV = "REAPER_MCP_EVIDENCE_DIR"
RUN_TASK_ENV = "REAPER_MCP_RUN_TASK"
VISION_DIR_ENV = "REAPER_VISION_DIR"

#: Task id used in the run directory name, so a vision session is recognisable
#: next to a test run of the same day.
SESSION_TASK = "vision"

_lock = threading.Lock()
_session_dir: Path | None = None


def _env(environ: dict | None = None) -> dict:
    return dict(os.environ if environ is None else environ)


def user_data_root(environ: dict | None = None) -> Path:
    """The user-local base directory (same meaning as the test harness).

    This is ``%LOCALAPPDATA%`` itself, NOT a project subdirectory: the harness
    defines it that way and `default_run_artifacts_root` is what adds
    ``reaper_reapy_mcp``. Mirroring the meaning matters because the pinning test
    compares the two functions directly.
    """
    env = _env(environ)
    local = env.get("LOCALAPPDATA") or env.get("XDG_CACHE_HOME")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def default_run_artifacts_root(environ: dict | None = None) -> Path:
    """Where runs store artifacts when nothing overrides it (pure: no mkdir)."""
    return user_data_root(environ) / "reaper_reapy_mcp" / "run_artifacts"


def run_artifacts_root(environ: dict | None = None) -> Path:
    """``REAPER_MCP_RUN_ARTIFACTS`` if set, else the user-local default.

    A blank override counts as unset, and ``~`` is expanded - both rules come
    from the harness, so a run started by a test and a run started by the server
    land in the same place.
    """
    env = _env(environ)
    override = env.get(RUN_ARTIFACTS_ENV)
    if override and str(override).strip():
        return Path(override).expanduser()
    return default_run_artifacts_root(env)


def unique_path(path: Path) -> Path:
    """A path that is not on disk yet: ``x.png`` -> ``x__2.png`` -> ``x__3.png``.

    A run directory is never reused (same rule as the test harness), and two
    shots inside the same second must not overwrite each other.
    """
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}__{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def new_run_dir(task: str | None = None, *, when: datetime | None = None,
                pid: int | None = None, root: Path | None = None,
                environ: dict | None = None) -> Path:
    """A fresh run directory ``<UTC stamp>__<task>__<pid>`` (created, not reused)."""
    env = _env(environ)
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    task_name = task or env.get(RUN_TASK_ENV) or SESSION_TASK
    base = root if root is not None else run_artifacts_root(env)
    directory = base / f"{stamp}__{task_name}__{pid if pid is not None else os.getpid()}"
    index = 2
    while directory.exists():
        directory = base / f"{stamp}__{task_name}__{pid if pid is not None else os.getpid()}__{index}"
        index += 1
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def shots_dir(environ: dict | None = None) -> Path:
    """Where this process writes its images.

    ``REAPER_VISION_DIR`` -> used verbatim; ``REAPER_MCP_EVIDENCE_DIR`` -> used
    verbatim (the same rule the harness applies to its evidence); otherwise the
    session's ``<run dir>/shots``.
    """
    global _session_dir
    env = _env(environ)
    explicit = env.get(VISION_DIR_ENV)
    if explicit:
        directory = Path(explicit)
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    evidence = env.get(EVIDENCE_DIR_ENV)
    if evidence:
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    with _lock:
        if _session_dir is None:
            _session_dir = new_run_dir(environ=env) / "shots"
        _session_dir.mkdir(parents=True, exist_ok=True)
        return _session_dir


def reset_session_dir() -> None:
    """Forget the cached session directory (used by tests)."""
    global _session_dir
    with _lock:
        _session_dir = None


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(kind: str, payload: dict, *, directory: Path | None = None,
               base: Path | None = None) -> Path:
    """Write one JSON artifact and return its path (never reusing a name)."""
    target_dir = Path(directory) if directory is not None else shots_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_stamp()}__{kind}" if base is None else f"{base.stem}__{kind}"
    path = unique_path(target_dir / f"{stem}.json")
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def write_shot(shot: dict, meta: dict, *, label: str, data: bytes | None = None,
               directory: Path | None = None) -> dict:
    """Write ``<stamp>__<label>.png`` plus its JSON sidecar; return both paths.

    The sidecar is the point: a PNG alone cannot say which TIME RANGE it shows,
    and an agent that cannot tell two frames apart by their content needs that
    answer from the file itself.
    """
    target_dir = Path(directory) if directory is not None else shots_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(label))[:48]
    base = f"{_stamp()}__{safe_label or 'shot'}"
    png_path = unique_path(target_dir / f"{base}.png")
    if data is None:
        from . import imaging

        data = imaging.encode_image(shot)
    png_path.write_bytes(data)
    meta = dict(meta)
    meta["png"] = str(png_path)
    meta["png_bytes"] = len(data)
    sidecar = write_json("meta", meta, directory=target_dir, base=png_path)
    meta["meta_json"] = str(sidecar)
    return meta
