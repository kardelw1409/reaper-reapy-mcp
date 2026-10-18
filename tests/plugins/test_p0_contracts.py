"""P0 contract tests: import discipline, tool tables, envelope, error taxonomy.

Critical tests (14 P0): P0-1 (side-effect-free import), P0-3 (name collision),
P0-4 (mutation defaults). The L0 boundary tests live in test_p0_boundary.py and
the profile schema table in test_p0_profile_schema.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins import setup_plugin_tools
from reaper_reapy_mcp.plugins import errors as plugin_errors
from reaper_reapy_mcp.plugins import tools as plugin_tools
from reaper_reapy_mcp.plugins.errors import InvalidValue, PluginError
from reaper_reapy_mcp.plugins.tools import DISPATCH, SPEC, TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# P0-1 - import is side-effect free
# ---------------------------------------------------------------------------

_SIDE_EFFECT_PROBE = r"""
import json
import re
import sys

sys.path.insert(0, __SRC__)

# Warm the PRE-EXISTING package chain first (importing `reaper_reapy_mcp`
# already imports the existing server modules and their host client). That is
# pre-existing behaviour outside the plugin layer and outside this phase's
# boundary; what is measured below is the plugin layer's own import.
import reaper_reapy_mcp  # noqa: F401

events = []


def hook(event, args):
    if event == "open" and args and isinstance(args[0], str):
        events.append(["open", args[0]])
    elif event.startswith("socket.") or event == "subprocess.Popen":
        events.append([event, repr(args)])


sys.addaudithook(hook)

import reaper_reapy_mcp.plugins as plugins  # noqa: F401
import reaper_reapy_mcp.plugins.host.contract as host_contract  # noqa: F401
import reaper_reapy_mcp.plugins.model as model  # noqa: F401
import reaper_reapy_mcp.plugins.profile as profile  # noqa: F401
import reaper_reapy_mcp.plugins.provider as provider  # noqa: F401
import reaper_reapy_mcp.plugins.tools as tools  # noqa: F401

plugin_modules = sorted(
    name for name in sys.modules if name.startswith("reaper_reapy_mcp.plugins")
)
opens = [path for event, path in events if event == "open"]
bad_opens = [
    path
    for path in opens
    if path.lower().endswith(".json")
    or any(part.lower() == "cache" for part in re.split(r"[\\/]+", path))
]
host_calls = [[event, payload] for event, payload in events if event != "open"]
print(json.dumps({
    "plugin_modules": plugin_modules,
    "bad_opens": bad_opens,
    "host_calls": host_calls,
    "n_events": len(events),
}))
"""


def test_plugin_package_import_is_side_effect_free():
    """P0-1: importing the plugin layer performs no host call and reads no
    registry/cache data. Measured in a fresh process with an audit hook, with
    the pre-existing package chain warmed first so the measurement is about the
    plugin layer (which adds no eager connection of its own - 19 Decision 4)."""
    proc = subprocess.run(
        [sys.executable, "-c", _SIDE_EFFECT_PROBE.replace("__SRC__", repr(str(SRC)))],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"probe failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    expected = {
        "reaper_reapy_mcp.plugins",
        "reaper_reapy_mcp.plugins.errors",
        "reaper_reapy_mcp.plugins.model",
        "reaper_reapy_mcp.plugins.profile",
        "reaper_reapy_mcp.plugins.provider",
        "reaper_reapy_mcp.plugins.tools",
        "reaper_reapy_mcp.plugins.host",
        "reaper_reapy_mcp.plugins.host.contract",
    }
    assert expected <= set(payload["plugin_modules"]), payload["plugin_modules"]
    assert payload["host_calls"] == [], payload["host_calls"]
    assert payload["bad_opens"] == [], payload["bad_opens"]


def test_plugins_package_exports_setup_function():
    assert callable(setup_plugin_tools)
    assert plugin_tools.SCHEMA_VERSION == 2


# ---------------------------------------------------------------------------
# Error taxonomy (05 §7 is a CLOSED list)
# ---------------------------------------------------------------------------

_EXPECTED_ERROR_CODES = {
    "PluginError",
    "NotConnected",
    "Wedged",
    "Unreadable",
    "HostContractUnavailable",
    "InstanceNotFound",
    "AmbiguousInstance",
    "StaleInstance",
    "WrongPlugin",
    "VersionMismatch",
    "UnsupportedControl",
    "InvalidValue",
    "NoConversion",
    "InvalidRoute",
    "InvalidSourceFamily",
    "UnsupportedOverride",
    "DependencyViolation",
    "IncompatibleSettings",
    "PlanError",
    "UnsupportedProvider",
    "TargetSliceAmbiguous",
    "TargetChanged",
    "IdentityContinuityUnprovable",
    "StateConflict",
    "SnapshotUnavailable",
    # P28: the providerless path has nothing verified to work from until the
    # addressed instance has been measured once (`plugin_prepare`).
    "DiscoveryArtifactMissing",
    "WriteNotApplied",
    "StateMismatch",
    "ReadbackMismatch",
    "WriteOrderViolation",
    "RollbackFailed",
    "RestoreIncomplete",
}


def test_error_taxonomy_is_the_closed_list():
    assert set(plugin_errors.ERROR_CODES) == _EXPECTED_ERROR_CODES


def test_every_error_serializes_code_message_detail():
    for code in sorted(_EXPECTED_ERROR_CODES):
        cls = getattr(plugin_errors, code) if code != "PluginError" else plugin_errors.PluginError
        exc = cls("boom", {"k": 1})
        assert exc.code == code
        payload = exc.to_dict()
        assert set(payload) == {"code", "message", "detail"}
        assert payload["code"] == code and payload["message"] == "boom"
        assert payload["detail"] == {"k": 1}
        assert json.dumps(payload)
    assert plugin_errors.error_code(InvalidValue("x")) == "InvalidValue"
    assert plugin_errors.error_code(ValueError("x")) is None
    assert issubclass(InvalidValue, PluginError)


# ---------------------------------------------------------------------------
# Tool tables (06; AUD-009/AUD-013)
# ---------------------------------------------------------------------------


def test_spec_declares_exactly_the_seven_tools():
    assert set(SPEC) == set(TOOL_NAMES)
    # 9 since P38 added `plugin_presets` (the plugin's own preset library), 10 since P40
    # added `plugin_wavetable` (building our OWN tables and reading a slot back), 11 since P41
    # added `plugin_sample` (the sampler as a channel: a WAV in, the engine switched on). The
    # name "seven" in this test is a historical label. The pin moves with the round,
    # deliberately: the count is a measurement of the surface.
    assert len(TOOL_NAMES) == 11
    assert all(name.startswith("plugin_") for name in TOOL_NAMES)


def test_dispatch_table_exists_and_only_references_declared_names():
    assert set(DISPATCH) <= set(TOOL_NAMES)
    with pytest.raises(InvalidValue):
        plugin_tools.register_dispatch("plugin_not_a_tool", lambda **kw: {})


def _existing_tool_names() -> list[str]:
    source = (SRC / "mcp_tools.py").read_text(encoding="utf-8")
    return re.findall(r'@mcp\.tool\("([^"]+)"\)', source)


def _manifest_tool_names() -> list[str]:
    """The E2 manifest tool registers from its own package, not mcp_tools.py."""
    source = (SRC / "reaper_reapy_mcp" / "manifest" / "__init__.py").read_text(encoding="utf-8")
    return re.findall(r'@mcp\.tool\("([^"]+)"\)', source)


def test_tool_names_disjoint_from_existing_host_tools():
    """P0-3: the plugin names intersect the existing tool set in NOTHING."""
    existing = _existing_tool_names()
    # 57 -> 59: the host surface gained two tools after the P34 pin was written
    # (the owner's `save_project`/`new_project_tab` work - a pre-existing drift,
    # found by P38; the pins describe the SOURCE, so they move with it).
    assert len(existing) == 59, f"expected 59 existing tools, found {len(existing)}"
    assert set(existing) & set(TOOL_NAMES) == set()
    # 59 classic + 1 manifest tool (apply_audiocpp_manifest, registered by the
    # manifest package since E2) + 11 plugin_* (8 until P38 added plugin_presets,
    # 9 until P40 added plugin_wavetable, 10 until P41 added plugin_sample) = the tools
    # the running server advertises.
    manifest = _manifest_tool_names()
    assert manifest == ["apply_audiocpp_manifest"], manifest
    assert len(set(existing) | set(TOOL_NAMES) | set(manifest)) == 71


def test_every_mutating_tool_declares_dry_run_true():
    """P0-4 / AUD-009: no mutating call applies without an explicit opt-in."""
    mutators = [name for name, spec in SPEC.items() if spec["destructive"]]
    assert mutators, "the audit must not pass vacuously: at least one mutator exists"
    for name in mutators:
        args = SPEC[name]["args"]
        assert "dry_run" in args, f"{name} is destructive but declares no dry_run argument"
        assert args["dry_run"]["default"] is True, f"{name} must default to dry_run=true"
    for name, spec in SPEC.items():
        if not spec["destructive"]:
            assert spec["mutating_ops"] == ()


# ---------------------------------------------------------------------------
# Envelope (06 §2)
# ---------------------------------------------------------------------------


def test_ok_envelope_shape():
    env = plugin_tools.ok_envelope("plugin_read", {"values": {}})
    assert env["tool"] == "plugin_read" and env["schema"] == 2 and env["ok"] is True
    assert env["data"] == {"values": {}}
    assert env["warnings"] == []
    assert "error" not in env
    assert json.dumps(env)


def test_error_envelope_carries_typed_error_and_optional_outcome():
    env = plugin_tools.error_envelope("plugin_write", InvalidValue("nope", {"range": [0, 1]}))
    assert env["ok"] is False and env["schema"] == 2
    assert env["error"]["code"] == "InvalidValue"
    assert env["error"]["detail"] == {"range": [0, 1]}
    outcome = {"stage": "preflight", "verdict": "REFUSED_BEFORE_ANY_WRITE"}
    env2 = plugin_tools.error_envelope("plugin_write", InvalidValue("nope"), outcome=outcome)
    assert env2["outcome"] == outcome


def test_docstring_declares_envelope_and_args():
    text = plugin_tools.synthesize_docstring("plugin_write")
    assert "dry_run" in text
    assert "destructive: True" in text
    assert "schema-2 envelope" in text


# ---------------------------------------------------------------------------
# Registration closure (13 §3) - probe with a recording stub, never a server
# ---------------------------------------------------------------------------


class _RecordingMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str):
        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


def test_registration_registers_all_seven_without_host_or_provider_config():
    mcp = _RecordingMcp()
    setup_plugin_tools(mcp)  # config absent on purpose
    assert set(mcp.tools) == set(TOOL_NAMES)
    for name, fn in mcp.tools.items():
        assert fn.__name__ == name
        assert fn.__doc__ == plugin_tools.synthesize_docstring(name)


def test_registration_closure_refuses_missing_required_argument():
    mcp = _RecordingMcp()
    setup_plugin_tools(mcp)
    env = mcp.tools["plugin_write"]()
    assert env["ok"] is False
    assert env["error"]["code"] == "InvalidValue"
    assert "instance" in env["error"]["message"] and "request" in env["error"]["message"]
    assert env["error"]["detail"]["required"] == ["instance", "request"]


def test_registration_closure_reports_missing_body_without_traceback():
    """The closure's behaviour when a body is absent (P9 attaches the production
    bodies during registration, so the absence is constructed after it)."""
    mcp = _RecordingMcp()
    setup_plugin_tools(mcp)
    saved = plugin_tools.DISPATCH.pop("plugin_read", None)
    try:
        env = mcp.tools["plugin_read"](instance={"host": "reaper"}, controls=[])
        assert env["ok"] is False
        assert env["error"]["code"] == plugin_tools.INTERNAL_ERROR_CODE
    finally:
        if saved is not None:
            plugin_tools.DISPATCH["plugin_read"] = saved


def test_registration_closure_dispatches_and_reports_typed_errors():
    mcp = _RecordingMcp()

    def _body(*, config, **kwargs):
        raise plugin_errors.StateConflict("stale", {"observed": "x"})

    setup_plugin_tools(mcp)
    saved = plugin_tools.DISPATCH.get("plugin_read")
    plugin_tools.DISPATCH["plugin_read"] = _body
    try:
        env = mcp.tools["plugin_read"](instance={}, controls=[])
        assert env["ok"] is False and env["error"]["code"] == "StateConflict"
    finally:
        if saved is not None:
            plugin_tools.DISPATCH["plugin_read"] = saved
        else:
            plugin_tools.DISPATCH.pop("plugin_read", None)


def test_registration_closure_reports_bad_arguments_with_accepted_list():
    mcp = _RecordingMcp()

    def _body(*, config, **kwargs):
        raise TypeError("unexpected keyword argument 'nope'")

    setup_plugin_tools(mcp)
    saved = plugin_tools.DISPATCH.get("plugin_state")
    plugin_tools.DISPATCH["plugin_state"] = _body
    try:
        env = mcp.tools["plugin_state"](instance={})
        assert env["ok"] is False
        assert env["error"]["code"] == "InvalidValue"
        assert "nope" in env["error"]["message"]
        assert "accepted" in env["error"]["detail"]
    finally:
        if saved is not None:
            plugin_tools.DISPATCH["plugin_state"] = saved
        else:
            plugin_tools.DISPATCH.pop("plugin_state", None)
