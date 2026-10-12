"""P29 stage B: an explicit normalized write, and its separation from semantics.

P28's write path had one mode: a value in **display** space, which needs a
*confirmed* conversion. A control whose law was never verified therefore made the
whole batch refuse - correct for a display request, wrong for a caller who states
the host's own normalized value. The two scenarios are now separate:

* a **display** request still needs its verified law (nothing about that relaxed);
* a **normalized** request needs no law at all, and the layer says so per row
  (`value_space`, `display_claim`) instead of rendering the normalized number as a
  display value.

Everything else the P28 path guarantees is exercised here as well: the batch stays
the only writer, the pre-flight refuses before any mutation, the index/name guard
runs before each setter, the read-back is compared in the space that was written,
and a partly unresolvable batch reports every refused entry per control.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import host_scope as HS
from reaper_reapy_mcp.plugins import request_value as RV
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins.errors import InvalidValue

import discovery_double as D  # noqa: E402
from test_p28_providerless import (  # noqa: E402
    INSTANCE,
    _materialize,
    build_bridge,
    call,
    prepared_env,  # noqa: F401 - the fixture is used through this import
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: The law-less control of the fixture (its display never changes) and the ladder
#: whose grid the values layer snaps onto.
LAWLESS = "m1"
LADDER = "octave"


@pytest.fixture
def providerless(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    bridge, directory = prepared_env(tmp_path, monkeypatch)
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=config)
    return {"bridge": bridge, "mcp": mcp, "config": config, "directory": directory, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# The contract of the value itself
# ---------------------------------------------------------------------------


def test_a_normalized_value_is_the_host_space_and_needs_no_law():
    control = {"id": "x", "type": "number", "law": {"kind": "none"}, "authority": {"write": "parameter"}, "binding": {"index": 4}}
    row = RV.resolve_control_value(control, {"normalized": 0.25})["row"]
    assert row["normalized"] == 0.25
    assert row["value_space"] == "normalized"
    assert row["display"] is None and row["native"] is None
    assert row["display_claim"]["available"] is False
    assert row["display_claim"]["reason"] == "explicit_normalized_request"


@pytest.mark.parametrize("value", [-0.5, 1.5, float("nan"), float("inf")])
def test_a_normalized_value_outside_the_host_space_is_refused(value):
    control = {"id": "x", "type": "number", "law": {"kind": "none"}, "authority": {"write": "parameter"}, "binding": {"index": 4}}
    problem = RV.resolve_control_value(control, {"normalized": value})["problem"]
    assert problem["code"] == "InvalidValue"
    assert problem["control"] == "x"


def test_a_normalized_value_must_be_a_number_not_a_boolean():
    control = {"id": "x", "type": "number", "law": {"kind": "none"}, "authority": {"write": "parameter"}, "binding": {"index": 4}}
    problem = RV.resolve_control_value(control, {"normalized": True})["problem"]
    assert problem["code"] == "InvalidValue"


def test_a_normalized_write_on_a_labelled_control_names_the_members():
    """A raw position inside a labelled band cannot be verified afterwards: the
    read-back could not say *which* label the plugin chose, so the request is
    refused and the labels are offered instead."""
    control = {
        "id": "type",
        "type": "enum",
        "law": {"kind": "enum_index", "params": {}},
        "authority": {"write": "parameter"},
        "enum_values": [{"id": "Off"}, {"id": "On"}],
        "binding": {"index": 2},
    }
    problem = RV.resolve_control_value(control, {"normalized": 0.5})["problem"]
    assert problem["code"] == "InvalidValue"
    assert problem["detail"]["members"] == ["Off", "On"]


def test_two_value_modes_in_one_entry_are_refused():
    control = {"id": "x", "type": "number", "law": {"kind": "none"}, "authority": {"write": "parameter"}, "binding": {"index": 4}}
    problem = RV.resolve_control_value(control, {"normalized": 0.5, "display": 1.0})["problem"]
    assert problem["code"] == "InvalidValue"
    assert problem["detail"]["accepted"] == list(RV.VALUE_MODES)


def test_an_unknown_value_key_is_refused_with_the_accepted_set():
    control = {"id": "x", "type": "number", "law": {"kind": "none"}, "authority": {"write": "parameter"}, "binding": {"index": 4}}
    problem = RV.resolve_control_value(control, {"raw": 0.5})["problem"]
    assert problem["code"] == "InvalidValue"
    assert problem["detail"]["accepted"] == list(RV.VALUE_MODES)


def test_a_native_value_is_never_taken_for_a_normalized_one():
    """`to_normalized` ends in an identity fallback when a control has neither a law
    nor a declared range - so a `native` request on a law-less control has to be
    refused, or the plugin's own value would be written as if it were the
    normalized one (an assumption wearing a conversion's name)."""
    control = {
        "id": "x",
        "type": "number",
        "law": {"kind": "none"},
        "authority": {"write": "parameter"},
        "binding": {"index": 4},
    }
    problem = RV.resolve_control_value(control, {"native": 1.0})["problem"]
    assert problem["code"] == "NoConversion"
    assert "normalized" in problem["detail"]["remedy"]


def test_a_scalar_keeps_its_historical_meaning_and_still_needs_the_law():
    control = {"id": "x", "type": "number", "law": {"kind": "none"}, "authority": {"write": "parameter"}, "binding": {"index": 4}}
    problem = RV.resolve_control_value(control, 0.5)["problem"]
    assert problem["code"] == "NoConversion"


def test_the_providerless_double_coercion_survives_the_shared_resolver():
    """The one coercion this path always had: a boolean word for a two-member
    enum. It is passed as a hook, so it is not applied to a normalized request."""
    from reaper_reapy_mcp.plugins import values as V

    control = {
        "id": "keytrack",
        "type": "enum",
        "law": {"kind": "enum_index", "params": {}},
        "authority": {"write": "parameter"},
        "enum_values": [{"id": "Off"}, {"id": "On"}],
        "binding": {"index": 5},
    }
    row = RV.resolve_control_value(
        control, True, values_module=V, coerce=lambda _c, v: "On" if v is True else v
    )["row"]
    assert row["display"] == "On"
    assert row["value_space"] == "display"


def test_the_batch_policy_is_validated_before_any_host_work():
    assert RV.normalize_request_mode({}) == "refuse"
    assert RV.normalize_request_mode({"on_unresolved": "skip"}) == "skip"
    with pytest.raises(InvalidValue):
        RV.normalize_request_mode({"on_unresolved": "maybe"})
    with pytest.raises(InvalidValue):
        RV.normalize_request_mode({"on_unresolved": True})


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def test_a_law_less_control_is_writable_by_explicit_normalized_request(providerless):
    """The whole point of the round: a missing semantic mapping no longer blocks a
    write whose value the caller states in the host's own space."""
    bridge = providerless["bridge"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: {"normalized": 0.72}}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["value_spaces"] == {"normalized": 1}
    assert data["outcome"]["host"]["write_calls_attempted"] == 1
    assert bridge.values[4] == pytest.approx(0.72)
    row = data["outcome"]["channels"]["parameters"]["rows"][0]
    assert row["written"] == pytest.approx(0.72)
    assert row["read_back"] == pytest.approx(0.72)
    assert row["agrees"] is True
    assert row["space"] == "normalized"
    # and it does NOT claim a display value it never had
    assert row["display_claim"]["available"] is False
    assert "display_expected" not in row and "display_read_back" not in row


def test_the_preview_shows_the_space_and_never_a_fake_display(providerless):
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: {"normalized": 0.4}}},
            "dry_run": True,
        },
    )
    assert envelope["ok"] is True, envelope
    preview = envelope["data"]["preview"]["parameters"][0]
    assert preview["value_space"] == "normalized"
    assert preview["normalized"] == pytest.approx(0.4)
    assert preview["stored_display"] is None
    item = envelope["data"]["plan"]["items"][0]
    assert item["value_space"] == "normalized"
    assert item["to"] == pytest.approx(0.4)


def test_a_normalized_write_reports_the_grid_the_plugin_will_hold(providerless):
    """A ladder is a *conversion*: the preview must show the value that lands, not
    the one that was asked for."""
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LADDER: {"normalized": 0.5}}},
            "dry_run": True,
        },
    )
    assert envelope["ok"] is True, envelope
    preview = envelope["data"]["preview"]["parameters"][0]
    assert preview["stored_display"] == 7.0, preview
    assert preview["normalized"] == pytest.approx(0.5)


def test_a_semantic_write_is_unchanged_and_still_needs_its_law(providerless):
    """The two scenarios are separate: display space still converts, and a display
    value is still refused where no law was verified."""
    bridge = providerless["bridge"]
    good = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": 1000.0}},
            "dry_run": False,
        },
    )
    assert good["ok"] is True, good
    assert good["data"]["value_spaces"] == {"display": 1}
    assert good["data"]["outcome"]["channels"]["parameters"]["rows"][0]["space"] == "normalized"

    bad = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: 0.5}},
            "dry_run": False,
        },
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "NoConversion"
    assert bad["outcome"]["host"]["write_calls_attempted"] == 0
    assert bridge.values[4] == 0.0, "a refused display write must not touch the plugin"


def test_a_native_request_converts_through_the_law_and_is_refused_without_one(providerless):
    bridge = providerless["bridge"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": {"native": 1000.0}}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["value_spaces"] == {"native": 1}
    written = {index: value for index, _name, value in bridge.index_write_rows}
    assert written[1] == pytest.approx((1000.0 - 20.0) / (20000.0 - 20.0), abs=1e-9)
    row = envelope["data"]["outcome"]["channels"]["parameters"]["rows"][0]
    assert row["display_read_back"] == pytest.approx(1000.0)

    refused = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: {"native": 1.0}}},
            "dry_run": True,
        },
    )
    assert refused["ok"] is False
    assert refused["error"]["code"] == "NoConversion"


def test_both_spaces_travel_in_one_batch_and_are_reported_per_row(providerless):
    bridge = providerless["bridge"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {
                "controls": {
                    "filter_1_cutoff": 800.0,
                    LAWLESS: {"normalized": 0.3},
                    LADDER: {"normalized": 0.5},
                }
            },
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    assert envelope["data"]["value_spaces"] == {"display": 1, "normalized": 2}
    assert envelope["data"]["outcome"]["host"]["write_calls_attempted"] == 1, "one batch, one write"
    written = {index: value for index, _name, value in bridge.index_write_rows}
    assert written[1] == pytest.approx((800.0 - 20.0) / (20000.0 - 20.0), abs=1e-9)
    assert written[4] == pytest.approx(0.3)
    assert envelope["data"]["display_checks"]["n_rows"] == 3
    rows = envelope["data"]["outcome"]["channels"]["parameters"]["rows"]
    by_control = {row["control"]: row for row in rows}
    assert by_control["filter_1_cutoff"]["display_read_back"] == pytest.approx(800.0)
    assert by_control[LAWLESS]["display_claim"]["available"] is False


def _labelled_volume_display(index: int, normalized: float) -> str:
    """A numeric ladder whose *minimum* prints a word - Surge's oscillator volume.

    Measured live (P29): `A Osc 1 Volume` prints `-inf dB` at 0 and numbers above it.
    The engine therefore fits a numeric law from the numeric samples while recording
    the word as a member, which is exactly the shape that needs `labelled_states`.
    """
    if float(normalized) < 0.03125:
        return "-inf dB"
    return f"{-72.0 + 72.0 * float(normalized):.2f} dB"


@pytest.fixture
def labelled(tmp_path, monkeypatch):
    """The fixture bridge plus one labelled numeric control (index 5)."""
    from mcp.server.fastmcp import FastMCP

    from reaper_reapy_mcp.plugins.discovery.pipeline import DiscoveryEngine, EngineConfig
    from reaper_reapy_mcp.plugins.discovery.store import PreparedStore
    from reaper_reapy_mcp.plugins.model import PluginInstanceRef
    from reaper_reapy_mcp.plugins.resolve import facts_location

    controls = [
        D.FakeControl(0, "Amp EG Attack", display=D.power_display(scale=4.0, exponent=2.0, unit=" s")),
        D.FakeControl(1, "Filter 1 Cutoff", display=D.linear_display(20.0, 20000.0, " Hz")),
        D.FakeControl(2, "Filter 1 Type", display=D.labels_display(["Off", "LP 24 dB"])),
        D.FakeControl(3, "Octave", display=build_bridge().controls[3].display),
        D.FakeControl(4, "M1: -", display=lambda _i, _n: "0.000000"),
        D.FakeControl(5, "A Osc 1 Volume", display=_labelled_volume_display),
    ]
    bridge = D.FakeDiscoveryBridge(controls)
    monkeypatch.setattr(A, "_BRIDGE", bridge)
    monkeypatch.setattr(A, "_REGISTRY", None)
    directory = tmp_path / "discovery"
    monkeypatch.setenv("REAPER_MCP_PLUGIN_DISCOVERY", str(directory))
    facts = bridge.fx_facts(0, 0)
    ref = PluginInstanceRef(
        host="reaper",
        location=facts_location(facts),
        provider="host-scope",
        identity=HS.HostScope().identify(facts),
        display="fake",
    )
    result = DiscoveryEngine(bridge=bridge).run(
        ref, facts, config=EngineConfig(mode="prepare", grid_points=17).resolved()
    )
    PreparedStore(directory=directory).save(result, identity=ref.identity)
    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=("vital",), env={}))
    return {"bridge": bridge, "mcp": mcp}


def test_a_labelled_state_is_written_at_its_measured_position_and_reported(labelled):
    """Surge's oscillator volumes in miniature: the request names the word, the plugin
    prints the word back, and the *law* thinks something else. `agrees` follows what
    the caller asked for; the law's own tally is reported beside it instead of being
    hidden (an independent review of this round found the headline counting only the
    request-space rows, i.e. staying silent about the law)."""
    bridge = labelled["bridge"]
    envelope = call(
        labelled["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"a_osc_1_volume": "-inf dB"}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    written = {index: value for index, _name, value in bridge.index_write_rows}
    assert written[5] == 0.0, "the position the label was measured at"
    row = envelope["data"]["outcome"]["channels"]["parameters"]["rows"][0]
    assert row["agrees"] is True
    checks = envelope["data"]["display_checks"]
    assert checks["n_rows"] == 1
    assert checks["n_disagreements"] == 0, "the request was honoured"
    assert checks["n_law_disagreements"] == 1, "and the law's opinion is reported, not hidden"
    assert checks["n_compared_with_request"] == 1
    detail = checks["rows"][0]
    assert detail["requested_display"] == "-inf dB"
    assert detail["host_display_read_back"] == "-inf dB"
    assert detail["agrees_with_law"] is False


def test_an_unknown_labelled_state_is_refused_with_the_options(labelled):
    envelope = call(
        labelled["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"a_osc_1_volume": "-999 dB"}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "UnsupportedControl"
    assert "-inf dB" in envelope["error"]["detail"]["problems"][0]["detail"]["states"]
    assert labelled["bridge"].index_write_rows == [], "nothing was written"


def test_the_preflight_still_refuses_the_whole_batch_by_default(providerless):
    bridge = providerless["bridge"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {"filter_1_cutoff": 100.0, LAWLESS: 0.5, LADDER: 0.5}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NoConversion"
    assert [row["control"] for row in envelope["error"]["detail"]["problems"]] == [LAWLESS]
    assert envelope["outcome"]["host"]["write_calls_attempted"] == 0
    assert bridge.values[1] == 0.0 and bridge.values[3] == 0.0 and bridge.values[4] == 0.0


def test_a_skip_batch_applies_the_resolved_part_and_names_every_refusal(providerless):
    """The explicit opt-in: the workable controls are written, the refused one
    keeps its own typed problem, and the response says the batch was partial."""
    bridge = providerless["bridge"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {
                "controls": {"filter_1_cutoff": 500.0, LAWLESS: 0.5},
                "on_unresolved": "skip",
            },
            "dry_run": False,
        },
    )
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["partial"] is True
    assert [row["control"] for row in data["refused"]] == [LAWLESS]
    assert data["refused"][0]["code"] == "NoConversion"
    # the count that matters is what was VERIFIED, not what was resolved: the review
    # of this round caught a version that reported the resolved set, which would
    # claim controls as applied on a batch that failed and rolled back
    assert data["partial_application"]["resolved"] == 1
    assert data["partial_application"]["verified"] == 1
    assert data["partial_application"]["refused"] == 1
    assert "applied" not in data["partial_application"]
    written = {index: value for index, _name, value in bridge.index_write_rows}
    assert written[1] == pytest.approx((500.0 - 20.0) / (20000.0 - 20.0), abs=1e-9)
    assert 4 not in written, "the refused control was never touched"
    assert bridge.values[3] == 0.0


def test_a_skip_batch_that_resolves_nothing_is_a_refusal_not_an_empty_success(providerless):
    bridge = providerless["bridge"]
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: 0.5}, "on_unresolved": "skip"},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NoConversion"
    assert bridge.values[4] == 0.0


def test_the_name_guard_runs_before_each_setter_for_a_normalized_row(providerless):
    """The identity check is a property of the *write*, not of the value's space."""
    bridge = providerless["bridge"]
    bridge.name_shift = {4: "Something Else"}
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: {"normalized": 0.9}}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "TargetChanged"
    assert envelope["error"]["detail"]["scene"] == "index_name_guard"
    assert envelope["error"]["detail"]["writes_issued"] == 0
    assert bridge.index_write_rows == [], "the setter must not have run"
    assert bridge.values[4] == 0.0


def test_a_normalized_write_that_does_not_land_is_still_a_failure(providerless):
    bridge = providerless["bridge"]
    bridge.refuse_writes = True
    envelope = call(
        providerless["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"controls": {LAWLESS: {"normalized": 0.8}}},
            "dry_run": False,
        },
    )
    assert envelope["ok"] is False
    assert envelope["outcome"]["host"]["write_calls_attempted"] >= 1
    assert envelope["outcome"]["verdict"].startswith("FAILED") or envelope["outcome"][
        "verdict"
    ].startswith("ROLLBACK")
    assert bridge.values[4] == 0.0, "the setter did not land, and nothing claims it did"


def test_the_requirements_and_pins_of_p28_are_untouched(providerless):
    """The provider path and the providerless refusal semantics must not move: the
    Vital provider still resolves its own requests, and a named provider still
    keeps its refusal on a providerless slot."""
    from reaper_reapy_mcp.plugins.providers.vital import provider as VP

    assert hasattr(VP.VitalProvider, "normalize_request")
    envelope = call(
        providerless["mcp"],
        "plugin_describe",
        {"instance": {**INSTANCE, "provider": "vital"}},
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] in ("InstanceNotFound", "TargetChanged")


def test_the_shared_resolver_is_what_the_providerless_path_uses(monkeypatch, providerless):
    """A pin on the wiring itself: if the providerless path ever stops resolving
    through the shared vocabulary, the two paths could drift apart silently."""
    seen: list[Any] = []
    original = RV.resolve_control_value

    def spy(control, value, **kwargs):
        seen.append((control.get("id"), value))
        return original(control, value, **kwargs)

    monkeypatch.setattr(HS.RV, "resolve_control_value", spy)
    call(
        providerless["mcp"],
        "plugin_write",
        {"instance": dict(INSTANCE), "request": {"controls": {LAWLESS: {"normalized": 0.2}}}},
    )
    assert seen == [(LAWLESS, {"normalized": 0.2})]


def test_the_materialized_profile_still_has_no_value_fields():
    """P29 must not have turned the profile into a value cache (P21's invariant)."""
    bridge = build_bridge()
    profile, controls = _materialize(bridge)
    for control in controls.values():
        for key in ("value", "current", "readback"):
            assert key not in control, f"{control.get('id')} carries {key!r}"
    assert profile["controls"], "the fixture must produce controls"
