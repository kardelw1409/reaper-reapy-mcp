"""Bridge transport boundary: no semantics in the bridge, no live write in the suite.

**Phase-gate migration.** In P3 this file asserted "no write verb exists" - that
was the P3 phase gate, and P5 is precisely the phase that adds the write
transport (`14-implementation-plan.md`, P5). The gate therefore moves to the
properties that must hold *now*:

1. the bridge module contains **no provider predicate, no verification
   callback, no tolerance and no plugin vocabulary** (AUD-004) - Core decodes
   and verifies through the provider codec, the bridge only moves bytes;
2. importing the module still performs no host I/O and no data-file read;
3. the transport is the only host importer inside `plugins/` and its import
   stays lazy;
4. the plugin suite cannot reach a live write: the conftest guard refuses it,
   and the guard is proven by calling it.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins.host import reaper as host_reaper

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
HERE = Path(__file__).resolve().parent

#: Tokens that would mean semantic knowledge leaked into the transport (AUD-004).
SEMANTIC_TOKENS = (
    "verify",
    "tolerance",
    "predicate",
    "plugin_id",
    "lfo",
    "wavetable",
    "osc_",
    "filter_1",
)

#: Verbs a write transported through this module must expose (P5).
WRITE_VERBS = ("write_params", "write_state_slice")


def _code_only(path: Path) -> str:
    """The module source with comments and string literals removed.

    A docstring that says *"contains no verification callback"* must not read
    as a verification callback, so the scan runs on code tokens only.
    """
    import io
    import tokenize

    pieces: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            pieces.append(token.string)
    return "\n".join(pieces)


def test_module_source_contains_no_semantic_callback():
    source = _code_only(Path(host_reaper.__file__))
    found = [
        token
        for token in SEMANTIC_TOKENS
        if re.search(rf"\b{re.escape(token)}", source, flags=re.IGNORECASE)
    ]
    assert found == [], f"the transport gained semantic knowledge: {found}"
    # non-vacuity: the stripper really strips - the raw file does contain the
    # word it was claimed to be free of, in the docstring that states the rule
    raw = Path(host_reaper.__file__).read_text(encoding="utf-8")
    assert "verify" in raw.lower()
    assert "verify" not in source.lower()


def test_module_imports_no_provider_package():
    tree = ast.parse(Path(host_reaper.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if "provider" in name.lower()], imported


def test_bridge_exposes_the_full_host_contract():
    """Both halves of the contract exist; the write half is the P5 addition."""
    bridge = host_reaper.ReaperHostBridge()
    for required in (
        "list_tracks",
        "fx_facts",
        "param_idents",
        "param_index_from_idents",
        "read_params",
        "read_state_chunk",
        "locate_state_slice",
        "call",
        "reconnect",
        *WRITE_VERBS,
        "re_read_slice",
    ):
        assert callable(getattr(bridge, required)), f"the contract needs {required}()"
    assert callable(host_reaper.splice_block_text)


def test_importing_the_read_bridge_performs_no_host_io():
    """A fresh process imports the module with no socket and no data-file read.

    The pre-existing package chain is warmed first (it performs host I/O by
    design - 19 Decision 4), so the measurement is about the plugin layer.
    """
    script = (
        "import json, re, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import reaper_reapy_mcp  # warm the pre-existing chain\n"
        "events = []\n"
        "def hook(event, args):\n"
        "    if event == 'open' and args and isinstance(args[0], str):\n"
        "        events.append(['open', args[0]])\n"
        "    elif event.startswith('socket.'):\n"
        "        events.append([event, repr(args)])\n"
        "sys.addaudithook(hook)\n"
        "from reaper_reapy_mcp.plugins.host import reaper  # noqa: F401\n"
        "opens = [p for kind, p in events if kind == 'open']\n"
        "bad = [p for p in opens if p.lower().endswith('.json') and '__pycache__' not in p]\n"
        "calls = [[k, p] for k, p in events if k != 'open']\n"
        "print(json.dumps({'bad': bad, 'host_calls': calls}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["host_calls"] == [], payload["host_calls"]
    assert payload["bad"] == [], payload["bad"]


def test_the_bridge_is_the_only_host_importer_inside_plugins():
    """The L0 whitelist now has exactly one real importer, and the module
    actually uses the lazy host client (so the whitelist is not vacuous)."""
    source = Path(host_reaper.__file__).read_text(encoding="utf-8")
    assert re.search(r"import reapy", source), "the bridge must import the host client somewhere"
    lines = source.splitlines()
    module_level = [line for line in lines if re.match(r"^(import|from)\s+reapy", line)]
    assert module_level == [], "the host import must be lazy (no module-level import)"


def test_live_write_gate_refuses_a_real_bridge_write():
    """The suite's live-write guard is real: a direct call is refused loudly."""
    bridge = host_reaper.ReaperHostBridge()
    ref = host_reaper.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="demo", identity={}
    )
    with pytest.raises(AssertionError) as excinfo:
        bridge.write_params(ref, [("0:1", 0.5)])
    assert "live host write" in str(excinfo.value)
    with pytest.raises(AssertionError):
        bridge.write_state_slice(
            ref, None, None, timing=host_reaper.TimingProfile()
        )


def test_no_test_file_pairs_a_real_bridge_with_a_write_verb():
    """A source-level guard on top of the runtime one: a test that binds a real
    `ReaperHostBridge` object must not call a write verb on **that object**
    (this file's own guard test triggers the runtime refusal deliberately, so it
    is excluded). The check is an AST pass over bindings, not a text scan: a mock
    host variable in the same file is not the real bridge.
    """
    offenders: list[str] = []
    for path in sorted(HERE.rglob("test_*.py")):
        if path == Path(__file__):
            continue
        offenders.extend(
            bridge_write_offenders(path.read_text(encoding="utf-8"), path.name)
        )
    assert offenders == [], (
        "these test sites call a write verb on a real bridge object: " f"{offenders}"
    )


def bridge_write_offenders(source: str, filename: str = "<source>") -> list[str]:
    """The scan behind the guard above, split out so it can be probed.

    A checker that has only ever run on a passing input is an untested claim
    (independent-audit finding F3), so `..._is_probed` below feeds it a source
    that must be reported.
    """
    offenders: list[str] = []
    tree = ast.parse(source, filename=filename)
    real_bridge_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            target_name = getattr(func, "id", None) or getattr(func, "attr", None)
            if target_name == "ReaperHostBridge":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        real_bridge_names.add(target.id)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in WRITE_VERBS:
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in real_bridge_names:
            offenders.append(f"{filename}:{node.lineno}:{node.func.attr}")
    return offenders


def test_the_real_bridge_write_scan_is_probed_and_not_vacuous():
    """The negative probe for the guard above (audit finding F3).

    Asserts the scan **reports** the very shape it exists to catch, and that it
    does not report a mock variable or a read verb in the same source.
    """
    offending = (
        "from reaper_reapy_mcp.plugins.host import reaper as host_reaper\n"
        "def test_x():\n"
        "    bridge = host_reaper.ReaperHostBridge()\n"
        "    bridge.write_params(ref, [('0:1', 0.5)])\n"
    )
    reported = bridge_write_offenders(offending, "synthetic_offender.py")
    assert reported == ["synthetic_offender.py:4:write_params"]

    clean = (
        "from reaper_reapy_mcp.plugins.host import reaper as host_reaper\n"
        "def test_y():\n"
        "    mock = MockHostBridge()\n"
        "    mock.write_params(ref, [('0:1', 0.5)])\n"
        "    real = host_reaper.ReaperHostBridge()\n"
        "    real.read_params(ref, ['0:1'])\n"
    )
    assert bridge_write_offenders(clean, "synthetic_clean.py") == []
