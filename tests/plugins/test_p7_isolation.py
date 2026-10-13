"""P7 V-12: the provider's import boundary is mechanical (AUD-005).

A **fresh process** imports the provider package and reports what that pulled in.
The assertions are about the provider's own transitive closure, so the process
must not warm the pre-existing package chain first (that chain imports the host
API by design - 19 Decision 4); it imports the provider module by file path.

Checked: no `reapy`, no `reascript_api`, no `ReaperController`, no `_bh_session`,
no `_b4_lib`, no other legacy module; the working directory is unchanged; and no
registry JSON is read at import time.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

LEGACY_MODULES = ("reapy", "reascript_api", "reaper_controller", "_bh_session", "_b4_lib", "_b3_lib", "vital_analysis")


def _run(script: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(SRC.parent),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_importing_the_provider_pulls_in_no_legacy_module_and_no_registry_read():
    """The pre-existing package chain is warmed first (it performs host I/O by
    design - 19 Decision 4), so the measurement is about the provider package's
    own import: nothing legacy, no registry read, no working-directory change.
    """
    script = (
        "import importlib, json, os, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import reaper_reapy_mcp  # warm the pre-existing chain\n"
        "before_cwd = os.getcwd()\n"
        "before = set(sys.modules)\n"
        "events = []\n"
        "def hook(event, args):\n"
        "    if event == 'open' and args and isinstance(args[0], str):\n"
        "        events.append(args[0])\n"
        "sys.addaudithook(hook)\n"
        "pkg = importlib.import_module('reaper_reapy_mcp.plugins.providers.vital')\n"
        "new_modules = sorted(set(sys.modules) - before)\n"
        "legacy_present = sorted(k for k in ['_b4_lib', '_bh_session', '_b3_lib', "
        "'vital_analysis', 'vital_dialog_guard', 'vital_render', 'reaper_controller'] "
        "if k in sys.modules)\n"
        "reapy_added = 'reapy' in (set(sys.modules) - before)\n"
        "top_level_new = sorted({m.split('.')[0] for m in new_modules} - {'reaper_reapy_mcp'})\n"
        "json_reads = [p for p in events if p.lower().endswith('.json') and '__pycache__' not in p]\n"
        "print(json.dumps({'legacy': legacy_present, 'reapy_added': reapy_added, "
        "'top_level_new': top_level_new, 'json_reads': json_reads, "
        "'cwd_changed': os.getcwd() != before_cwd, "
        "'has_provider': hasattr(pkg, 'VitalProvider'), 'has_codec': hasattr(pkg, 'VitalCodec')}))\n"
    )
    payload = _run(script)
    assert payload["legacy"] == [], payload["legacy"]
    assert payload["reapy_added"] is False, (
        "the provider import must not be what pulls the host client in"
    )
    assert payload["top_level_new"] == [], payload["top_level_new"]
    assert payload["json_reads"] == [], payload["json_reads"]
    assert payload["cwd_changed"] is False
    assert payload["has_provider"] and payload["has_codec"]


def test_constructing_the_provider_still_reads_no_registry_until_first_use():
    """Configuration is read on first use: a provider object is inert."""
    script = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from reaper_reapy_mcp.plugins.providers.vital import VitalProvider\n"
        "events = []\n"
        "def hook(event, args):\n"
        "    if event == 'open' and args and isinstance(args[0], str):\n"
        "        events.append(args[0])\n"
        "sys.addaudithook(hook)\n"
        "provider = VitalProvider()\n"
        "paths = provider.artifact_paths()\n"
        "json_reads = [p for p in events if p.lower().endswith('.json') and '__pycache__' not in p]\n"
        "print(json.dumps({'n_structs': len(paths), 'json_reads': json_reads}))\n"
    )
    payload = _run(script)
    # 10 since P30: `wavetables.py` joined the provider's behaviour-affecting files
    # (a new file changes the artifact digest whether or not it is listed, so the list
    # must name it or the declared source rows would under-count the key material).
    assert payload["n_structs"] == 10
    assert payload["json_reads"] == [], (
        "constructing the provider must not read the registry (08 §6.4)"
    )


def test_in_process_boundary_checker_still_passes_for_the_provider_package():
    """The L0 checker exempts `providers/` from the identifier ban but still bans
    host imports there; this asserts the exemption is real and the ban holds."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import l0_boundary

    plugins_root = SRC / "reaper_reapy_mcp" / "plugins"
    files, violations = l0_boundary.check_boundaries(plugins_root)
    assert files, "non-vacuity: the checker must have examined files"
    assert violations == [], violations
    provider_files = [path for path in files if "providers" in path.as_posix()]
    assert provider_files, "the provider package must be inside the checked tree"
