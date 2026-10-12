"""P9: the 7 tools exercised through the **registered FastMCP surface** (mocks).

Contract 13 §3 / 06 §10: one server, unconditional registration, and every tool
answering a schema-2 envelope. These tests do not call the Python bodies
directly - they call `mcp.call_tool(...)` on a real `FastMCP` instance with the
same registration call `create_server()` uses, so the argument validation, the
envelope and the dispatch path are all exercised. The host is the mock bridge and
the provider is a demo provider registered under a test id, which keeps the suite
offline (`REAPER` closed) while still travelling the whole pipeline.

The MCP server's *own* construction is covered separately: `create_server()`
builds a `ReaperController`, whose `__init__` performs the pre-existing eager
`reapy.connect()` (19 Decision 4 / AUD-006 Option A). That is a live property and
is confirmed by the P9 live smoke test, not here.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import fixtures as F
import mock_host as MH
from mock_provider import MockProvider

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins.providers import registry as PR

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def build_host(*, reset_params: dict[str, float] | None = None) -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    return MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [foreign, MH.MockFxSlot()])],
        reset_params_on_state_write=reset_params,
    )


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A registered server over a mock host and a demo provider, fully isolated."""
    from mcp.server.fastmcp import FastMCP

    host = build_host()
    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "demo")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(A, "_BRIDGE", host)
    monkeypatch.setattr(A, "_REGISTRY", None)
    factory = {"make": lambda: MockProvider()}
    monkeypatch.setitem(PR._FACTORIES, "demo", lambda: factory["make"]())
    config = A.PluginConfig(
        provider_ids=("demo",), cache_dir=str(tmp_path / "cache"), env={}
    )
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=config)
    yield {"host": host, "mcp": mcp, "config": config, "factory": factory}


def call(mcp, name: str, arguments: dict | None = None) -> dict:
    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


def tool_names(mcp) -> list[str]:
    return sorted(tool.name for tool in asyncio.run(mcp.list_tools()))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_registered_server_exposes_all_seven_plugin_tools():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp)
    assert tool_names(mcp) == sorted(T.TOOL_NAMES)
    schemas = {tool.name: sorted((tool.inputSchema or {}).get("properties", {})) for tool in asyncio.run(mcp.list_tools())}
    for name in T.TOOL_NAMES:
        assert schemas[name] == sorted(T.SPEC[name]["args"]), name
        tool = next(item for item in asyncio.run(mcp.list_tools()) if item.name == name)
        assert tool.description and "envelope" in tool.description


def test_adding_the_plugin_layer_leaves_the_existing_tools_identical():
    """Additivity (P9-2/P9-3): the host tool set is the same object before/after.

    The removal path is structural (one call in `create_server`), so what is
    asserted here is the property that matters: registering the plugin layer
    changes nothing about the tools that were registered first.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("probe")

    def host_tool_one() -> dict:
        return {"status": "success"}

    def host_tool_two(value: int = 0) -> dict:
        return {"status": "success", "value": value}

    mcp.tool(name="reaper_track_list")(host_tool_one)
    mcp.tool(name="reaper_track_get")(host_tool_two)
    before = {
        tool.name: json.dumps(tool.inputSchema, sort_keys=True)
        for tool in asyncio.run(mcp.list_tools())
    }
    T.setup_plugin_tools(mcp)
    after_tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    after = {name: json.dumps(tool.inputSchema, sort_keys=True) for name, tool in after_tools.items()}
    assert set(after) == set(before) | set(T.TOOL_NAMES)
    for name, schema in before.items():
        assert after[name] == schema, name
        assert after_tools[name].description == ""

    # the pre-existing tools still answer their own shape, untouched by the layer
    blocks = asyncio.run(mcp.call_tool("reaper_track_list", {}))
    assert json.loads(blocks[0].text) == {"status": "success"}


def test_zero_providers_answer_a_typed_refusal_that_never_measures_or_writes(monkeypatch, tmp_path):
    """P28 contract (replacing AUD-013's "never touches the host"): with no provider
    configured the four control tools answer the providerless path's refusal, and
    the properties that matter are sharper than before - a request may resolve the
    addressed slot (a read-only identity read) and then refuses with the remedy;
    it must never **measure** the plugin nor write to it. The discovery engine is
    booby-trapped here, so an accidental sweep fails loudly.
    """
    import discovery_double as D
    from reaper_reapy_mcp.plugins.discovery import pipeline as DISCOVERY
    from reaper_reapy_mcp.plugins.errors import DiscoveryArtifactMissing

    def bomb(*_args, **_kwargs):  # pragma: no cover - it must never run
        raise AssertionError("a read/describe/find/write request triggered a measurement")

    monkeypatch.setattr(DISCOVERY.DiscoveryEngine, "run", bomb)
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(tmp_path / "discovery"))
    bridge = D.FakeDiscoveryBridge([D.FakeControl(0, "alpha_gain")])
    monkeypatch.setattr(A, "_BRIDGE", bridge)
    monkeypatch.setattr(A, "_REGISTRY", None)
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=(), env={}))

    # `plugin_instances` lists *provider* instances: with none configured and no
    # selector it refuses before any host work, exactly as before.
    envelope = call(mcp, "plugin_instances", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "UnsupportedProvider"
    assert envelope["error"]["detail"]["variable"] == "REAPER_MCP_PLUGIN_PROVIDERS"

    # ...but a caller that NAMES the slot gets an answer: an enumeration needs a
    # vocabulary, a selector does not, and the host-scope block reports readiness
    # from the prepared artifact without measuring (the engine above is trapped).
    selected = call(mcp, "plugin_instances", {"selector": {"track_index": 0, "fx_index": 0}})
    assert selected["ok"] is True, selected
    assert selected["data"]["instances"] == []
    assert [row["artifact"]["status"] for row in selected["data"]["host_scope"]] == ["absent"]
    assert selected["meta"]["profile_source"] == "discovery"
    assert [row["code"] for row in selected["warnings"]] == ["NoProviderConfigured"]

    instance = {"track_index": 0, "fx_index": 0}
    cases = {
        "plugin_describe": {"instance": dict(instance)},
        "plugin_find": {"instance": dict(instance)},
        "plugin_read": {"instance": dict(instance), "controls": ["alpha_gain"]},
        "plugin_write": {"instance": dict(instance), "request": {"controls": {"alpha_gain": 1.0}}},
        "plugin_state": {"instance": dict(instance)},
        "plugin_modulation": {"instance": dict(instance), "op": "list"},
    }
    for name, arguments in cases.items():
        envelope = call(mcp, name, arguments)
        assert envelope["ok"] is False, name
        assert envelope["error"]["code"] == "DiscoveryArtifactMissing", (name, envelope)
        assert envelope["error"]["detail"]["remedy"].startswith("call plugin_prepare")
        assert envelope["schema"] == 2 and envelope["tool"] == name

    assert bridge.calls["param_metadata"] == 0, "no request may sweep the plugin"
    assert bridge.calls["format_params_normalized"] == 0, "no request may sweep the display"


def test_an_unknown_provider_id_refuses_with_the_known_ids():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=("nope",), env={}))
    envelope = call(mcp, "plugin_instances", {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "UnsupportedProvider"
    assert envelope["error"]["detail"]["known_providers"] == list(PR.known_provider_ids())


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def test_plugin_instances_lists_the_instance_and_issues_a_token(demo):
    envelope = call(demo["mcp"], "plugin_instances", {})
    assert envelope["ok"] is True
    instances = envelope["data"]["instances"]
    assert len(instances) == 1, instances
    ref = instances[0]
    assert ref["provider"] == "demo"
    assert ref["location"] == {"track_index": 0, "fx_index": 1}
    assert ref["identity"]["class_id"] == F.DEMO_CLASS
    assert envelope["data"]["ambiguous"] is False
    assert envelope["meta"]["target_token"].startswith("tok_")
    assert len(demo["host"].calls("fx_facts")) >= 2


def test_plugin_describe_reports_identity_profile_and_counts(demo):
    envelope = call(demo["mcp"], "plugin_describe", {"instance": {"track_index": 0, "fx_index": 1}})
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["modules"] and data["counts"]["controls"] == len(F.demo_profile()["controls"])
    assert data["counts"]["enums"] == 1
    assert data["routes"]["slots"] == F.ROUTE_SLOTS
    assert data["profile"]["key"]
    assert envelope["meta"]["cache"]["source"] in ("cache", "built")
    full = call(demo["mcp"], "plugin_describe", {"instance": {"track_index": 0, "fx_index": 1}, "detail": "full"})
    assert any("controls" in module for module in full["data"]["modules"])


def test_plugin_find_matches_controls_and_groups(demo):
    envelope = call(demo["mcp"], "plugin_find", {"instance": {"track_index": 0, "fx_index": 1}, "query": "cutoff"})
    assert envelope["ok"] is True
    assert [row["id"] for row in envelope["data"]["matches"]] == ["tone_cutoff"]
    empty = call(demo["mcp"], "plugin_find", {"instance": {"track_index": 0, "fx_index": 1}, "query": "zzz"})
    assert empty["ok"] is True and empty["data"]["matches"] == [] and empty["data"]["suggestions"] == []
    grouped = call(demo["mcp"], "plugin_find", {"instance": {"track_index": 0, "fx_index": 1}, "group": "tone"})
    assert {row["id"] for row in grouped["data"]["matches"]} == {"tone_cutoff", "tone_level", "tone_shape"}
    unknown = call(demo["mcp"], "plugin_find", {"instance": {"track_index": 0, "fx_index": 1}, "group": "nope"})
    assert unknown["ok"] is False and unknown["error"]["code"] == "InvalidValue"


def test_plugin_read_is_one_batched_hop_and_reports_problems(demo):
    instance = {"track_index": 0, "fx_index": 1}
    demo["host"].reset_log()
    envelope = call(
        demo["mcp"],
        "plugin_read",
        {"instance": instance, "controls": ["tone_cutoff", "no_such_control", "mode_state"]},
    )
    assert envelope["ok"] is True, envelope
    assert demo["host"].count("read_params") == 1, "one batched hop for both readable controls"
    assert envelope["data"]["values"]["tone_cutoff"]["normalized"] == pytest.approx(0.50)
    assert envelope["data"]["values"]["tone_cutoff"]["unit"] == "semitones"
    problems = {row["control"]: row["code"] for row in envelope["data"]["problems"]}
    assert problems == {"no_such_control": "UnsupportedControl", "mode_state": "UnsupportedControl"}


def test_plugin_state_inspect_reports_the_selected_slice(demo):
    envelope = call(demo["mcp"], "plugin_state", {"instance": {"track_index": 0, "fx_index": 1}})
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["counts"]["routes"] == len(F.demo_document()["routes"])
    assert envelope["data"]["state"]["n_active_routes"] == 2, "the demo document declares two routes"
    assert envelope["data"]["slice"]["class_hex"] == F.DEMO_CLASS
    assert envelope["data"]["slice"]["ordinal"] == 0
    deferred = call(demo["mcp"], "plugin_state", {"instance": {"track_index": 0, "fx_index": 1}, "op": "restore"})
    assert deferred["ok"] is False
    assert deferred["error"]["code"] == "InvalidValue"
    assert deferred["error"]["detail"]["accepted"] == ["inspect"]


# ---------------------------------------------------------------------------
# Write tool: the pipeline, the dry-run default and the gates
# ---------------------------------------------------------------------------


def test_plugin_write_defaults_to_dry_run_and_writes_nothing(demo):
    instance = {"track_index": 0, "fx_index": 1}
    envelope = call(
        demo["mcp"], "plugin_write", {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}}
    )
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["dry_run"] is True
    assert envelope["outcome"]["verdict"] == "NO_WRITES"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert demo["host"].count("write_params") == 0
    assert demo["host"].count("write_state_slice") == 0


def test_plugin_write_applies_only_with_an_explicit_dry_run_false(demo):
    instance = {"track_index": 0, "fx_index": 1}
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": False},
    )
    assert envelope["ok"] is True, envelope
    assert envelope["outcome"]["verdict"] == "ALL_VERIFIED"
    assert envelope["outcome"]["final_state"] == "committed"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 1
    assert envelope["outcome"]["host"]["write_effects_verified"] == 1
    rows = envelope["outcome"]["channels"]["parameters"]["rows"]
    assert rows and rows[0]["agrees"] is True
    assert demo["host"].slot(0, 1).params["3:1003"] == pytest.approx(0.24)


def test_plugin_write_cannot_bypass_the_version_gate(demo):
    demo["factory"]["make"] = lambda: MockProvider(document_version=lambda _document: "9.9.9")
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {
            "instance": {"track_index": 0, "fx_index": 1},
            "request": {"controls": {"tone_cutoff": 24}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "VersionMismatch"
    assert envelope["error"]["detail"]["override"] is None
    assert envelope["outcome"]["verdict"] == "REFUSED_BEFORE_ANY_WRITE"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert demo["host"].count("write_params") == 0


def test_plugin_write_refuses_an_unknown_control_with_zero_writes(demo):
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": {"track_index": 0, "fx_index": 1}, "request": {"controls": {"nope": 1}}, "dry_run": False},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "UnsupportedControl"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0


@pytest.mark.parametrize("value", [0, 1, "false", "", [], {}])
def test_a_falsy_looking_non_boolean_can_never_opt_a_caller_into_a_write(demo, value):
    """Audit finding F4, at the registered surface.

    ``bool("false")`` is ``True``: without a strict check the classic
    falsy-looking string would have applied the write for real. The transport
    declares these parameters as free-form, so the refusal has to be the
    engine's, and it has to happen before any host write.
    """
    instance = {"track_index": 0, "fx_index": 1}
    envelope = call(
        demo["mcp"],
        "plugin_write",
        {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}, "dry_run": value},
    )
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["code"] == "InvalidValue"
    assert envelope["error"]["detail"]["argument"] == "dry_run"
    assert demo["host"].count("write_params") == 0
    assert demo["host"].count("write_state_slice") == 0
    assert demo["host"].slot(0, 1).params["3:1003"] != pytest.approx(0.24)


# ---------------------------------------------------------------------------
# Modulation
# ---------------------------------------------------------------------------


def test_plugin_modulation_list_reads_and_create_is_dry_run_by_default(demo):
    instance = {"track_index": 0, "fx_index": 1}
    listed = call(demo["mcp"], "plugin_modulation", {"instance": instance, "op": "list"})
    assert listed["ok"] is True and listed["data"]["n_slots_total"] == F.ROUTE_SLOTS
    assert listed["data"]["routes"], "the demo document declares two routes"
    preview = call(
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": instance,
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}],
        },
    )
    assert preview["ok"] is True, preview
    assert preview["meta"]["dry_run"] is True
    assert preview["outcome"]["verdict"] == "NO_WRITES"
    assert preview["outcome"]["host"]["write_calls_attempted"] == 0
    assert demo["host"].count("write_state_slice") == 0


def test_plugin_modulation_create_applies_with_explicit_apply(demo):
    instance = {"track_index": 0, "fx_index": 1}
    applied = call(
        demo["mcp"],
        "plugin_modulation",
        {
            "instance": instance,
            "op": "create",
            "routes": [{"source": "src_a", "destination": "tone_level", "amount": 0.5}],
            "dry_run": False,
        },
    )
    assert applied["ok"] is True, applied
    assert applied["outcome"]["verdict"] == "ALL_VERIFIED"
    assert demo["host"].count("write_state_slice") == 1


def test_plugin_modulation_ambiguous_selector_refuses_a_mutation(demo):
    envelope = call(
        demo["mcp"],
        "plugin_modulation",
        {"instance": {"track_index": 0, "fx_index": 1}, "op": "remove", "selector": {}, "dry_run": False},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "InvalidRoute"
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert demo["host"].count("write_state_slice") == 0


# ---------------------------------------------------------------------------
# Envelope discipline across the whole surface
# ---------------------------------------------------------------------------


def test_every_answer_is_a_schema_two_envelope_and_never_carries_the_token(demo):
    instance = {"track_index": 0, "fx_index": 1}
    calls = [
        ("plugin_instances", {}),
        ("plugin_describe", {"instance": instance}),
        ("plugin_find", {"instance": instance, "query": "tone"}),
        ("plugin_read", {"instance": instance, "controls": ["tone_cutoff"]}),
        ("plugin_write", {"instance": instance, "request": {"controls": {"tone_cutoff": 24}}}),
        ("plugin_state", {"instance": instance}),
        ("plugin_modulation", {"instance": instance, "op": "list"}),
    ]
    seen_tokens = set()
    for name, arguments in calls:
        envelope = call(demo["mcp"], name, arguments)
        assert envelope["schema"] == 2 and envelope["tool"] == name, name
        assert ("ok" in envelope) and ("warnings" in envelope)
        assert ("data" in envelope) is bool(envelope["ok"])
        assert ("error" in envelope) is (not envelope["ok"])
        if envelope["ok"] and name != "plugin_instances":
            assert "target_token" in envelope["meta"]
            seen_tokens.add(envelope["meta"]["target_token"])
        payload = json.dumps({key: value for key, value in envelope.items() if key != "meta"})
        for token in seen_tokens:
            assert token not in payload, (name, "the token leaked into data/outcome")
    assert len(seen_tokens) == 1, "one target, one session token"
