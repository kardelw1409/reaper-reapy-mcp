"""P30 — the wavetable channel: semantic op, guard, verification, whole-slot rollback.

The capability exists because of a measured fact about this plugin build: the table an
oscillator reads is **document state** with no host parameter behind it (the only
wave-related controls are `*_wave_frame`, the position INSIDE a table), and the state
channel is the only way to change it. What these tests pin is not "a table can be
written" - it is the three things that make such a write safe:

1. the **guard** (schema, no growth beyond what the engine holds, `version` stamped from
   the live document, the slot must exist) fires on the semantic op AND on the raw state
   path, because the raw path is a way to reach the same slot;
2. the **verification row** reports content identities (and, when it disagrees, WHICH
   field) rather than two multi-kilobyte blobs;
3. the **rollback** expresses a slot as one whole-table change, never as its leaves -
   a leaf write inside a slot is refused by the same guard.

Mock host + fixtures only: no live REAPER, no C:\\REAPER.
"""

from __future__ import annotations

import base64
import json
import struct

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import api as A
from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import tools as T
from reaper_reapy_mcp.plugins.errors import InvalidValue, Unreadable
from reaper_reapy_mcp.plugins.providers.vital import codec as VC
from reaper_reapy_mcp.plugins.providers.vital import wavetables as WT

import test_p7_vital as T7

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

INSTANCE = {"track_index": 0, "fx_index": 0}
LIVE_VERSION = "1.5.5"


# ---------------------------------------------------------------------------
# Table/document builders (the document's own shape, nothing invented)
# ---------------------------------------------------------------------------


def wave_data(samples: list[float]) -> str:
    return base64.b64encode(struct.pack("<%df" % len(samples), *samples)).decode("ascii")


def frame(position: int, samples: list[float]) -> dict:
    return {"position": position, "wave_data": wave_data(samples)}


def table(samples: list[float], *, name: str = "Init", version: str = "1.0.7", frames=None) -> dict:
    keyframes = frames or [frame(0, samples)]
    return {
        "author": "",
        "full_normalize": False,
        "name": name,
        "remove_all_dc": False,
        "version": version,
        "groups": [
            {
                "components": [
                    {
                        "type": "Wave Source",
                        "interpolation": 1,
                        "interpolation_style": 1,
                        "keyframes": keyframes,
                    }
                ]
            }
        ],
    }


def saw(n: int = 8) -> list[float]:
    return [2.0 * (i / n) - 1.0 for i in range(n)]


def sine(n: int = 8) -> list[float]:
    import math

    return [math.sin(2.0 * math.pi * i / n) for i in range(n)]


def document(*tables: dict) -> dict:
    doc = T7.document()
    doc["synth_version"] = LIVE_VERSION
    doc["settings"]["wavetables"] = [dict(row) for row in (tables or (table(saw()),) * 3)]
    return doc


def preset_file(tmp_path, tables: list[dict], *, name: str = "preset.vital", header: bytes = b"\x01\x02"):
    path = tmp_path / name
    path.write_bytes(header + json.dumps({"author": "P30", "settings": {"wavetables": tables}}).encode("utf-8"))
    return str(path)


def vital_transaction(request: dict, *, dry_run: bool, doc: dict | None = None):
    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [vitals_slot(doc=doc)])])
    return transaction_on(host, request, dry_run=dry_run), host


def vitals_slot(*, doc: dict | None = None) -> MH.MockFxSlot:
    """The Vital slot with the LIVE version, so the tool-level version gate passes."""
    slot = T7.vitals_slot(block=T7.build_block(doc if doc is not None else document()))
    slot.version = LIVE_VERSION
    return slot

def transaction_on(host, request: dict, *, dry_run: bool):
    provider = T7.provider_on_fixtures()
    profile = provider.build_profile(dict(T7.IDENTITY))
    registry = T7.TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location=dict(INSTANCE), provider="vital", identity=T7.IDENTITY
    )
    return T7.Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request=dict(request),
        token=token,
        registry=registry,
        dry_run=dry_run,
    ).run()


def decode_chunk(host) -> dict:
    chunk = host.track(0).chunk()
    slice_ = T7.build_block  # noqa: F841 - readability anchor
    from reaper_reapy_mcp.plugins.host.reaper import locate_block_span, parse_blocks

    span = locate_block_span(chunk, class_hex=T7.IDENTITY["class_id"], module=T7.IDENTITY["module"], ordinal=0)
    framing = VC.parse_block_framing(span.block_text)
    return json.loads(framing["B"][framing["B"].find(b"{", 1):].decode("utf-8", "replace").split("\x00")[0])


# ---------------------------------------------------------------------------
# 1. The guard: the semantic op and the raw path reach the same slot
# ---------------------------------------------------------------------------


def test_the_guard_recognises_the_paths_the_codec_writes():
    assert WT.parse_wavetable_path(WT.path_for(1)) == {
        "path": "settings.wavetables.0",
        "index": 0,
        "oscillator": 1,
        "suffix": [],
        "is_slot": True,
    }
    assert WT.parse_wavetable_path(WT.path_for(3))["oscillator"] == 3
    # a deeper path is still *inside* a slot (that is what makes it a refusal, not a bypass)
    assert WT.parse_wavetable_path("settings.wavetables.1.groups.0")["suffix"] == ["groups", "0"]
    # anything else is not the guard's subject
    for quiet in ("settings.modulations.0.source", "settings.osc_1_wave_frame", "settings.wavetables", "wave_frame"):
        assert WT.parse_wavetable_path(quiet) is None, quiet


def test_a_non_growing_sine_table_replaces_the_saw_end_to_end(tmp_path):
    """The accepted path: preset file -> semantic spec -> verified document write."""
    sine_table = table(sine(), name="Sine Source")
    preset = preset_file(tmp_path, [sine_table])
    result, host = vital_transaction(
        {"wavetable": [{"oscillator": 1, "from_preset": preset, "preset_slot": 1}]},
        dry_run=False,
    )
    assert result.ok is True, result.error and result.error.to_dict()
    assert result.outcome.verdict is M.Verdict.ALL_VERIFIED
    rows = [row for row in result.outcome.channels["state"].rows if row.get("kind") == "wavetable"]
    assert len(rows) == 1, result.outcome.channels["state"].rows
    row = rows[0]
    assert row["path"] == "settings.wavetables.0"
    assert row["agrees"] is True
    assert row["differing_fields"] == []
    assert row["table_id_before"] == WT.table_id(table(saw()))
    assert row["table_id_written"] == WT.table_id(sine_table)
    assert row["table_id_read_back"] == row["table_id_written"], "the read-back is the table that was written"
    assert row["stamped_version_to"] == LIVE_VERSION, "the value is stamped with the live plugin's version"
    assert row["source"] and "preset.vital" in row["source"]
    # and the host really holds it: decode the chunk the mock host stored
    stored = decode_chunk(host)["settings"]["wavetables"][0]
    assert WT.table_id(stored) == WT.table_id(sine_table)
    assert stored["version"] == LIVE_VERSION


def test_the_raw_state_path_is_guarded_too(tmp_path):
    """A caller cannot bypass the guard by writing the dotted path directly."""
    preset = preset_file(tmp_path, [table(sine())])
    key = f"settings.wavetables.{WT.SETTINGS_KEY and 0}"
    sine_table = table(sine())
    ok, host = vital_transaction({"state": {key: sine_table}}, dry_run=False)
    assert ok.ok is True, ok.error and ok.error.to_dict()
    assert [row for row in host.log if row[0] == "write_state_slice"]

    # ... and a GROWING table through the same raw path is refused before any write
    growing = table(sine(), frames=[frame(0, sine()), frame(128, sine())])
    refused, host2 = vital_transaction({"state": {key: growing}}, dry_run=False)
    assert refused.ok is False
    assert refused.error.code == "InvalidValue"
    assert "growing payload" in refused.error.message or "larger" in refused.error.message
    assert refused.outcome.host.write_calls_attempted == 0
    assert [row for row in host2.log if row[0].startswith("write")] == []


def test_a_growing_table_is_refused_on_the_dry_run_too(tmp_path):
    preset = preset_file(tmp_path, [table(sine())])
    growing = table(sine(), frames=[frame(0, sine()), frame(128, sine())])
    result, host = vital_transaction(
        {"wavetable": [{"oscillator": 1, "from_preset": preset}]}, dry_run=True
    )
    assert result.ok is True, "the fixture preset is non-growing: the dry run must be available"
    refused, _host = vital_transaction({"state": {"settings.wavetables.0": growing}}, dry_run=True)
    assert refused.ok is False and refused.error.code == "InvalidValue"
    detail = refused.error.detail
    assert detail["growth_in_frames"] == 1
    assert detail["current"]["n_frames"] == 1
    assert "aborted" in json.dumps(detail), "the refusal carries the measurement that produced the bound"


def test_a_table_without_wave_frames_is_refused():
    codec = T7.provider_on_fixtures().codec()
    doc = document()
    payload = {"name": "Processing Only", "groups": [{"components": [{"type": "Wave Folder"}]}]}
    with pytest.raises(InvalidValue) as exc:
        codec.plan_state_writes(doc, {"paths": {"settings.wavetables.0": payload}})
    assert "Wave Source" in exc.value.message
    assert exc.value.detail["component_types"] == ["Wave Folder"]


def test_a_write_inside_a_slot_is_refused():
    codec = T7.provider_on_fixtures().codec()
    with pytest.raises(InvalidValue) as exc:
        codec.plan_state_writes(document(), {"paths": {"settings.wavetables.0.groups": []}})
    assert "inside" in exc.value.message
    assert exc.value.detail["slot_path"] == "settings.wavetables.0"


def test_a_slot_the_document_does_not_hold_is_refused():
    codec = T7.provider_on_fixtures().codec()
    doc = document()
    del doc["settings"]["wavetables"][2]
    with pytest.raises(InvalidValue) as exc:
        codec.plan_state_writes(doc, {"paths": {"settings.wavetables.2": table(sine())}})
    assert "does not address wavetable slot" in exc.value.message
    assert exc.value.detail["slots_in_document"] == 2


def test_an_unstampable_write_is_refused():
    """Without a live `synth_version` the plugin's restamp cannot be predicted."""
    codec = T7.provider_on_fixtures().codec()
    doc = document()
    doc.pop("synth_version")
    with pytest.raises(InvalidValue) as exc:
        codec.plan_state_writes(doc, {"paths": {"settings.wavetables.0": table(sine())}})
    assert exc.value.detail["why"] == "version_stamping_impossible"


def test_the_version_is_stamped_and_a_differing_readback_names_the_field():
    codec = T7.provider_on_fixtures().codec()
    doc = document()
    plan = codec.plan_state_writes(doc, {"paths": {"settings.wavetables.0": table(sine(), version="1.0.7")}})
    unit = plan.units[0]
    written = unit.payload["paths"]["settings.wavetables.0"]
    assert written["version"] == LIVE_VERSION
    note = unit.payload["wavetable"][0]
    assert note["stamped_version_from"] == "1.0.7"
    assert note["stamped_version_to"] == LIVE_VERSION
    assert note["table_id_before"] != note["table_id_after"]

    policy = T7.provider_on_fixtures().verification_policy()
    # the plugin's own re-serialisation (it writes its version, which is what we stamped)
    readback = {"document": {"settings": {"wavetables": [json.loads(json.dumps(written))]}}, "settings": {}}
    report = codec.verify(readback, unit, policy)
    assert report.ok is True, report.mismatched

    # ... and the POC's measured failure mode: an unstamped value would disagree ONLY
    # on `version` - which the row must NAME rather than dump two blobs
    stale = json.loads(json.dumps(written))
    stale["version"] = "1.0.7"
    readback["document"]["settings"]["wavetables"] = [stale]
    report = codec.verify(readback, unit, policy)
    assert report.ok is False
    assert report.mismatched[0]["differing_fields"] == ["version"]
    assert report.mismatched[0]["kind"] == "wavetable"


# ---------------------------------------------------------------------------
# 2. The provider's spec vocabulary
# ---------------------------------------------------------------------------


def normalize(request: dict):
    provider = T7.provider_on_fixtures()
    profile = provider.build_profile(dict(T7.IDENTITY))
    return provider.normalize_request(profile, dict(request))


def test_the_spec_resolves_to_a_state_path_with_provenance(tmp_path):
    sine_table = table(sine(), name="Sine Source")
    preset = preset_file(tmp_path, [table(saw()), sine_table])
    resolved = normalize({"wavetable": [{"oscillator": 1, "from_preset": preset, "preset_slot": 2}]})
    assert resolved.problems == []
    assert list(resolved.state) == ["settings.wavetables.0"]
    assert WT.table_id(resolved.state["settings.wavetables.0"]) == WT.table_id(sine_table)
    row = resolved.wavetable[0]
    assert row["oscillator"] == 1 and row["preset_slot"] == 2
    assert row["table_id"] == WT.table_id(sine_table)
    assert row["n_frames"] == 1 and row["payload_bytes"] > 0
    # the provenance rides the plan's change set
    from reaper_reapy_mcp.plugins import plan as P

    assert P.state_changes(resolved)["wavetable"][0]["path"] == "settings.wavetables.0"


def test_a_mismatched_table_id_is_refused(tmp_path):
    preset = preset_file(tmp_path, [table(sine())])
    resolved = normalize(
        {"wavetable": [{"oscillator": 1, "from_preset": preset, "table_id": "deadbeef"}]}
    )
    assert resolved.state == {}
    assert resolved.problems[0]["code"] == "InvalidValue"
    assert "does not match the requested table_id" in resolved.problems[0]["message"]


def test_spec_refusals_are_typed_and_name_the_accepted_keys(tmp_path):
    preset = preset_file(tmp_path, [table(sine())])
    cases = [
        ({"wavetable": [{"oscillator": 0, "from_preset": preset}]}, "no wavetable slot"),
        ({"wavetable": [{"oscillator": 1}]}, "needs `from_preset`"),
        ({"wavetable": [{"oscillator": 1, "from_preset": preset, "slot": 2}]}, "unknown wavetable spec field"),
        ({"wavetable": [{"oscillator": 1, "from_preset": str(tmp_path / "missing.vital")}]}, "no preset file"),
    ]
    for request, needle in cases:
        resolved = normalize(request)
        assert resolved.state == {}, request
        assert resolved.problems, request
        assert needle in resolved.problems[0]["message"], (request, resolved.problems[0])
        assert resolved.problems[0]["code"] in ("InvalidValue", "Unreadable"), resolved.problems[0]
        assert resolved.problems[0]["detail"], "a refusal carries structured evidence"


def test_a_file_that_is_not_a_vital_document_is_unreadable(tmp_path):
    broken = tmp_path / "broken.vital"
    broken.write_bytes(b"\x00\x01 not a document at all")
    resolved = normalize({"wavetable": [{"oscillator": 1, "from_preset": str(broken)}]})
    assert resolved.problems[0]["code"] == "Unreadable"


def test_two_changes_cannot_race_for_one_slot(tmp_path):
    preset = preset_file(tmp_path, [table(sine())])
    resolved = normalize(
        {
            "state": {"settings.wavetables.0": table(sine())},
            "wavetable": [{"oscillator": 1, "from_preset": preset}],
        }
    )
    assert resolved.problems[0]["detail"]["reason"] == "duplicate_slot"
    assert list(resolved.state) == ["settings.wavetables.0"], "the raw path keeps the slot"

    twice = normalize(
        {
            "wavetable": [
                {"oscillator": 1, "from_preset": preset},
                {"oscillator": 1, "from_preset": preset},
            ]
        }
    )
    assert twice.problems[0]["detail"]["reason"] == "duplicate_slot"


# ---------------------------------------------------------------------------
# 3. Rollback: a slot is restored as ONE whole-table change
# ---------------------------------------------------------------------------


def test_the_rollback_restores_the_slot_as_a_whole_table():
    codec = T7.provider_on_fixtures().codec()
    captured = document()
    snapshot = codec.snapshot_document(captured)
    live = document()
    live["settings"]["wavetables"][0] = table(sine())  # somebody replaced osc 1
    changes = codec.restore_changes(live, snapshot)
    paths = changes["paths"]
    assert "settings.wavetables.0" in paths, paths
    assert WT.table_id(paths["settings.wavetables.0"]) == WT.table_id(table(saw()))
    # no leaf INSIDE a slot survives (they would be refused by the guard)
    assert not [key for key in paths if WT.parse_wavetable_path(key) and key != "settings.wavetables.0"]
    # a slot that already matches is not rewritten at all
    assert "settings.wavetables.1" not in paths
    # and the change set is plannable: the guard runs on it without refusing
    plan = codec.plan_state_writes(live, changes)
    assert len(plan.units) == 1
    assert plan.units[0].payload["wavetable"][0]["path"] == "settings.wavetables.0"


# ---------------------------------------------------------------------------
# 4. The tool surface: plugin_state's view and the request the spec rides in
# ---------------------------------------------------------------------------


@pytest.fixture
def vital_demo(tmp_path, monkeypatch):
    """The registered server over a mock host with a Vital instance and its provider."""
    from mcp.server.fastmcp import FastMCP

    from reaper_reapy_mcp.plugins.providers import registry as PR

    host = MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [vitals_slot()])])
    monkeypatch.setenv("REAPER_MCP_PLUGIN_PROVIDERS", "vital")
    monkeypatch.setenv("REAPER_MCP_PLUGIN_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(A, "_BRIDGE", host)
    monkeypatch.setattr(A, "_REGISTRY", None)
    monkeypatch.setitem(PR._FACTORIES, "vital", lambda: T7.provider_on_fixtures())
    config = A.PluginConfig(provider_ids=("vital",), cache_dir=str(tmp_path / "cache"), env={})
    mcp = FastMCP("p30")
    T.setup_plugin_tools(mcp, config=config)
    return {"host": host, "mcp": mcp, "config": config}


def call(mcp, name: str, arguments: dict | None = None) -> dict:
    import asyncio

    blocks = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = next(block.text for block in blocks if getattr(block, "text", None))
    return json.loads(text)


def test_plugin_state_attaches_the_wavetable_view(vital_demo):
    envelope = call(vital_demo["mcp"], "plugin_state", {"instance": dict(INSTANCE)})
    assert envelope["ok"] is True, envelope
    view = envelope["data"].get("state_view")
    assert view is not None, envelope["data"].keys()
    wavetables = view["wavetables"]
    assert wavetables["present"] is True and wavetables["n_slots"] == 3
    first = wavetables["slots"][0]
    assert first["table_id"] == WT.table_id(table(saw()))
    assert first["n_frames"] == 1 and first["has_wave_source"] is True
    assert json.dumps(wavetables).find("wave_data") == -1, "the view never carries wave data"


def test_a_provider_without_the_hook_still_answers_the_summary(vital_demo, monkeypatch):
    """Non-vacuity: the view is additive - a provider without it is unchanged."""
    provider = T7.provider_on_fixtures()
    monkeypatch.delattr(type(provider), "state_view", raising=False)
    from reaper_reapy_mcp.plugins.providers import registry as PR

    monkeypatch.setitem(PR._FACTORIES, "vital", lambda: provider)
    monkeypatch.setattr(A, "_REGISTRY", None)
    envelope = call(vital_demo["mcp"], "plugin_state", {"instance": dict(INSTANCE)})
    assert envelope["ok"] is True
    assert "state_view" not in envelope["data"]


def test_the_write_tool_accepts_the_spec_and_reports_the_wavetable_plan(tmp_path, vital_demo):
    preset = preset_file(tmp_path, [table(sine())])
    envelope = call(
        vital_demo["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"wavetable": [{"oscillator": 1, "from_preset": preset}]},
            "dry_run": True,
        },
    )
    assert envelope["ok"] is True, envelope
    preview = envelope["data"]["preview"]
    assert preview["wavetable"][0]["path"] == "settings.wavetables.0"
    assert preview["wavetable"][0]["table_id"] == WT.table_id(table(sine()))
    assert envelope["data"]["plan_summary"]["n_wavetables"] == 1
    assert envelope["data"]["would_write"] is True

    applied = call(
        vital_demo["mcp"],
        "plugin_write",
        {
            "instance": dict(INSTANCE),
            "request": {"wavetable": [{"oscillator": 1, "from_preset": preset}]},
            "dry_run": False,
        },
    )
    assert applied["ok"] is True, applied
    rows = applied["data"]["outcome"]["channels"]["state"]["rows"]
    assert [row for row in rows if row.get("kind") == "wavetable"][0]["agrees"] is True
