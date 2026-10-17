"""The plugin layer's own run log: JSON lines, swept on a retention window.

Owner's ask (2026-10-17): a tool call that takes minutes has to be visible **while it
runs**, and the log must not grow without bound - old files are removed at server start
and nothing else is touched.

Design notes, each one deliberate:

* **Where**: the same user-local convention the rest of the layer uses
  (`%LOCALAPPDATA%\\reaper_reapy_mcp\\logs`, `$XDG_CACHE_HOME` elsewhere), overridable
  with `REAPER_MCP_LOG_DIR`. A log is machine-local, regenerable user data, so it never
  goes near the repository (P32 policy).
* **Shape**: one JSON object per line (`mcp-YYYY-MM-DD.log`). A line is self-describing
  and greppable; a reader does not need this module to interpret it.
* **Never in the way**: every function swallows its own failure. A log that cannot be
  written must never turn a working tool call into an error - the layer's contract is
  about the plugin, not about its own diagnostics.
* **Sweep scope**: only files matching our own `mcp-*.log` pattern inside our own
  directory. An ignore rule or a `*.log` glob is a claim about files we did not create.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "LOG_DIR_ENV",
    "RETENTION_ENV",
    "DEFAULT_RETENTION_HOURS",
    "LOG_FILE_PREFIX",
    "log_directory",
    "log_file",
    "sweep",
    "event",
    "record",
]

LOG_DIR_ENV = "REAPER_MCP_LOG_DIR"
RETENTION_ENV = "REAPER_MCP_LOG_RETENTION_HOURS"
DEFAULT_RETENTION_HOURS = 24.0
LOG_FILE_PREFIX = "mcp-"

#: One lock for the whole process: tool calls arrive on several threads.
_LOCK = threading.Lock()
#: The epoch (seconds) of the last sweep in this process, so a long-lived server sweeps
#: when the retention window rolls over instead of once per write.
_LAST_SWEEP = 0.0
_SWEEP_INTERVAL_S = 3600.0


def _user_root(env: Mapping[str, str]) -> Path:
    local = env.get("LOCALAPPDATA") or env.get("XDG_CACHE_HOME")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def log_directory(env: Mapping[str, str] | None = None) -> Path:
    """The log directory (explicit override first, else user-local)."""
    environ = os.environ if env is None else env
    override = environ.get(LOG_DIR_ENV)
    if override is not None and str(override).strip():
        return Path(os.path.normpath(os.path.abspath(str(override))))
    return Path(os.path.normpath(os.path.abspath(str(_user_root(environ) / "reaper_reapy_mcp" / "logs"))))


def retention_hours(env: Mapping[str, str] | None = None) -> float:
    environ = os.environ if env is None else env
    raw = environ.get(RETENTION_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_RETENTION_HOURS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_HOURS
    return value if value > 0 else DEFAULT_RETENTION_HOURS


def log_file(*, directory: Path | None = None, now: datetime | None = None) -> Path:
    """Today's log file (one per UTC day - a file per day is easy to sweep and to read)."""
    target = Path(directory) if directory is not None else log_directory()
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return target / f"{LOG_FILE_PREFIX}{stamp}.log"


def sweep(
    *,
    directory: Path | None = None,
    retention_hours_value: float | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Delete OUR log files older than the retention window; never raise.

    Only `mcp-*.log` inside our own directory is considered: a broader rule would make
    a claim about files this layer did not create.
    """
    target = Path(directory) if directory is not None else log_directory(env)
    window = (
        float(retention_hours_value)
        if retention_hours_value is not None
        else retention_hours(env)
    )
    removed: list[str] = []
    kept = 0
    failed: list[str] = []
    try:
        if not target.is_dir():
            return {"directory": str(target), "removed": [], "kept": 0, "failed": [], "retention_hours": window}
        cutoff = time.time() - window * 3600.0
        for path in sorted(target.glob(f"{LOG_FILE_PREFIX}*.log")):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path.name)
                else:
                    kept += 1
            except OSError as exc:
                failed.append(f"{path.name}: {exc.__class__.__name__}")
    except Exception as exc:  # noqa: BLE001 - a sweep must never be the reason a call fails
        return {
            "directory": str(target),
            "removed": removed,
            "kept": kept,
            "failed": failed + [f"{exc.__class__.__name__}: {exc}"],
            "retention_hours": window,
        }
    return {"directory": str(target), "removed": removed, "kept": kept, "failed": failed, "retention_hours": window}


def sweep_if_due(*, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Sweep at most once per hour per process (the server-start call is explicit)."""
    global _LAST_SWEEP
    now = time.time()
    if now - _LAST_SWEEP < _SWEEP_INTERVAL_S:
        return None
    _LAST_SWEEP = now
    return sweep(env=env)


def record(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> Path | None:
    """Append one event as a JSON line. Never raises: a log is not a contract.

    Sweeping rides along (at most hourly) so a long-running server keeps its own window
    even if it is never restarted.
    """
    try:
        sweep_if_due(env=env)
        path = log_file(directory=log_directory(env))
        line = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        return path
    except Exception:  # noqa: BLE001 - see the module docstring
        return None


def event(name: str, /, **fields: Any) -> Path | None:
    """One named event with a UTC timestamp and (when present) a duration."""
    payload: dict[str, Any] = {
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": name,
    }
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return record(payload)
