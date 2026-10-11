"""P7: the Vital provider, its ported codec and its registry loader.

Two kinds of evidence are used, and they are labelled as such in each test:

- **pinned fixtures** (`tests/plugins/fixtures/vital_*`) - small self-describing
  subsets of the real registry plus the *recorded measurements* of the live state
  chunk (`b4_state_blob.json`). The suite needs no `C:\\REAPER`.
- **recorded expectations** from the POC (the `modulation_depth("filter_1_cutoff", 8)
  == 0.0625` example, the recorded refusal set, the byte-identity round trip).

Nothing here writes to a host: the transport is the mock bridge, and the conftest
gate refuses a real-bridge write outright.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

import mock_host as MH

from reaper_reapy_mcp.plugins import model as M
from reaper_reapy_mcp.plugins import cache as C
from reaper_reapy_mcp.plugins.errors import (
    DependencyViolation,
    IncompatibleSettings,
    InvalidValue,
    NoConversion,
)
from reaper_reapy_mcp.plugins.host import reaper as R
from reaper_reapy_mcp.plugins.profile import validate_profile
from reaper_reapy_mcp.plugins.providers.vital import depth as D
from reaper_reapy_mcp.plugins.providers.vital import dependencies_rules as RULES
from reaper_reapy_mcp.plugins.providers.vital import (
    DEFAULT_REGISTRY_DIR,
    IMPLEMENTATION_REVISION,
    SUPPORTED_VERSIONS,
    VitalProvider,
    load_registry,
)
from reaper_reapy_mcp.plugins.providers.vital import codec as VC
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry
from reaper_reapy_mcp.plugins.transaction import Transaction

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REGISTRY_DIR = FIXTURES / "vital_registry"
CODEC_DIR = FIXTURES / "vital_codec"
IDENTITY = {
    "fx_name": "VST3i: Vital (Vital Audio)",
    "class_id": "56535456697461766974616C00000000",
    "module": "Vital.vst3",
    "param_count": 8,
}
HOST_PARAMS = {f"{index}:{1000 + index}": 0.25 for index in range(8)}

#: The names the mock host reports, i.e. what the live binding check compares a
#: declared control against (P8). They are the **fixture registry's own names**
#: for the indices inside the mock's 8-parameter table, so the binding the
#: engine measures is the same kind of evidence the live instance provides.
HOST_PARAM_NAMES = {
    0: "Beats Per Minute",
    1: "Chorus Filter Cutoff",
    2: "Chorus Delay 1",
    3: "Chorus Delay 2",
    4: "Chorus Mix",
    5: "Chorus Feedback",
    6: "(no control at index 6)",
    7: "(no control at index 7)",
}


# ---------------------------------------------------------------------------
# Block fixture built from the recorded measurements
# ---------------------------------------------------------------------------


def layout() -> dict:
    return json.loads((CODEC_DIR / "state_layout.json").read_text(encoding="utf-8"))


def document() -> dict:
    return json.loads((CODEC_DIR / "document.json").read_text(encoding="utf-8"))


def build_block(doc: dict, *, header: str | None = None) -> str:
    """A Vital state block with the recorded layout (prefix + JSON + trailer)."""
    spec = layout()
    prefix = bytes.fromhex(spec["item_b_prefix_hex"])
    trailer = bytes.fromhex(spec["item_b_trailer_hex"])
    body = prefix + json.dumps(doc, separators=(",", ":")).encode("utf-8") + trailer
    item_a = bytearray(bytes.fromhex(spec["item_a_hex"]))
    item_a[0x20:0x24] = struct.pack("<I", len(body))
    head = header or (
        '<VST "VST3i: Vital (Vital Audio)" Vital.vst3 0 "" '
        "56535456697461766974616C00000000 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0>"
    )
    lines = [head, base64.b64encode(bytes(item_a)).decode("ascii")]
    lines.extend(VC.wrap_lines(base64.b64encode(body).decode("ascii"), spec["b64_width"]))
    lines.append(spec["b64_artifact"])
    lines.append(">")
    return "\n".join(lines)


def vitals_slot(*, block: str | None = None) -> MH.MockFxSlot:
    return MH.MockFxSlot(
        name=IDENTITY["fx_name"],
        module=IDENTITY["module"],
        class_id=IDENTITY["class_id"],
        block=block or build_block(document()),
        params=dict(HOST_PARAMS),
        param_names=dict(HOST_PARAM_NAMES),
    )


def vital_host(*, extra_before=None, after=None) -> MH.MockHostBridge:
    slots: list[MH.MockFxSlot] = []
    if extra_before is not None:
        slots.append(extra_before)
    slots.append(vitals_slot())
    if after is not None:
        slots.append(after)
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", slots)])


def provider_on_fixtures() -> VitalProvider:
    return VitalProvider(registry=load_registry(directory=str(REGISTRY_DIR)))


def slice_for(host: MH.MockHostBridge, *, fx_index: int = 0):
    facts = host.fx_facts(0, fx_index)
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": fx_index}, provider="vital", identity=IDENTITY
    )
    chunk = host.read_state_chunk(ref)
    return host.locate_state_slice(ref, facts, chunk), ref, facts, chunk


# ---------------------------------------------------------------------------
# V-2 / codec: the recorded byte layout survives a rebuild
# ---------------------------------------------------------------------------


def test_codec_parse_rebuild_is_byte_identical_to_the_recorded_layout():
    spec = layout()
    block = build_block(document())
    framing = VC.parse_block_framing(block)
    assert framing["b64_width"] == spec["b64_width"] == 280
    assert framing["b64_artifact"] == spec["b64_artifact"], (
        "the artifact REAPER appends beyond the clean encoding must be identifiable"
    )
    assert framing["declared_len_b"] == len(framing["B"]), (
        "item A's recorded length field is the authority for item B's true length"
    )
    decoded, brace = VC.decode_document(framing["B"])
    assert brace == spec["json_offset_in_B"], "the JSON sits at the recorded offset"
    assert decoded == document()
    rebuilt = VC.rebuild_block_text(framing, decoded)
    assert rebuilt == block, "an unchanged document must rebuild byte-for-byte"


def test_codec_artifact_line_is_reappended_not_dropped():
    """Non-vacuity for the artifact: without the appended line the rebuild differs."""
    block = build_block(document())
    framing = VC.parse_block_framing(block)
    rebuilt = VC.rebuild_block_text(framing, document())
    with_artifact = rebuilt.count("\n") + 1
    assert with_artifact == block.count("\n") + 1
    # the recorded POC failure mode: dropping the artifact loses a line and 8 chars
    dropped = rebuilt.replace(f"\n{framing['b64_artifact']}\n", "\n")
    assert dropped.count("\n") + 1 == with_artifact - 1
    assert len(dropped) == len(rebuilt) - len(framing["b64_artifact"]) - 1


def test_codec_grow_updates_both_recorded_length_fields():
    block = build_block(document())
    framing = VC.parse_block_framing(block)
    doc = document()
    slot = VC.find_free_slot(doc)
    assert slot == 2, "slot 1 carries the pinned document's own route"
    VC.make_routing(doc, "lfo_1", "filter_1_cutoff", 0.0625, slot=slot)
    grown = VC.rebuild_block_text(framing, doc)
    new_framing = VC.parse_block_framing(grown)
    assert struct.unpack("<I", new_framing["A"][0x20:0x24])[0] == len(new_framing["B"])
    assert struct.unpack("<I", new_framing["B"][0:4])[0] == len(new_framing["B"]) - 16
    assert len(new_framing["B"]) > len(framing["B"]), "a created route grows the document"
    redoc, _ = VC.decode_document(new_framing["B"])
    assert redoc["settings"]["modulations"][slot - 1]["destination"] == "filter_1_cutoff"
    assert redoc["settings"][f"modulation_{slot}_amount"] == 0.0625


def test_codec_pad_mode_keeps_the_document_start_and_round_trips():
    """Live-detected (P8): a pad rewrite must not hide the document's own start.

    A shortening write preserves item B's length by inserting whitespace. The
    ported code inserted it right after the opening brace, which removed the
    ``{"author`` marker from the document's bytes - and the live decoder then
    found a **nested** JSON string inside the state instead. The padding now goes
    before the closing brace: the length fields stay valid, the marker survives,
    and the padded block decodes back to the same document.
    """
    block = build_block(document())
    framing = VC.parse_block_framing(block)
    doc = document()
    json_offset = framing["B"].find(b"{")
    assert json_offset >= 0
    # shrink the document: an emptied route destination makes the JSON shorter
    slots = doc["settings"]["modulations"]
    slot = next(index for index, row in enumerate(slots) if row.get("destination"))
    slots[slot]["destination"] = ""

    padded_block = VC.rebuild_block_text(framing, doc)
    padded = VC.parse_block_framing(padded_block)
    assert len(padded["B"]) == len(framing["B"]), "the pad form preserves item B's length"
    assert struct.unpack("<I", padded["A"][0x20:0x24])[0] == len(padded["B"])
    assert padded["B"][json_offset : json_offset + 8] == b'{"author', (
        "the document's opening bytes must survive the padding"
    )
    obj, brace = VC.decode_document(padded["B"])
    assert brace == json_offset
    assert obj == doc, "the padded block must decode back to the same document"
    assert obj["settings"]["modulations"][slot]["destination"] == ""


def test_codec_decode_never_returns_a_nested_json_string():
    """The measured trap: a nested wavetable document that also starts with `author`."""
    doc = document()
    nested = json.dumps(
        {
            "author": "",
            "full_normalize": True,
            "groups": [{"components": []}],
            "name": "wavetable",
            "version": 1,
        },
        separators=(",", ":"),
    )
    doc["settings"]["nested_meta"] = nested
    block = build_block(doc)
    framing = VC.parse_block_framing(block)
    brace0 = framing["B"].find(b"{")
    # the *old* padding shape, reproduced byte for byte: whitespace after the brace
    padded = framing["B"][:brace0] + b"{" + b" " * 8 + framing["B"][brace0 + 1 :]
    obj, brace = VC.decode_document(padded)
    assert brace == brace0
    assert isinstance(obj.get("settings"), dict), "the outer document, never the nested one"
    assert obj == doc
    assert obj["settings"]["nested_meta"] == nested, "the nested JSON stays a string"


def test_codec_derived_leaves_are_refused_and_excluded_from_a_restore():
    """Live-detected (P8): the plugin re-serializes `settings.sample.samples`.

    Writing a captured value for that leaf cannot land (measured: the plugin's own
    serialization comes back), so the codec declares it derived: an explicit write
    refuses **before** any write, and a restore excludes it while *reporting* the
    exclusion - a restore must never demand a leaf it cannot write, and must never
    quietly claim to have restored one.
    """
    assert VC.DERIVED_LEAVES == ("settings.sample.samples",)
    doc = document()
    doc["settings"]["sample"] = {"samples": "QUJD"}  # small stand-in for the blob
    block = build_block(doc)
    framing = VC.parse_block_framing(block)
    codec = VC.VitalCodec()
    slice_ = M.TargetedStateSlice(
        chunk_digest=M.text_digest(block),
        line_start=0,
        line_end=framing["line_end"],
        header_line=framing["header"],
        class_hex=IDENTITY["class_id"],
        ordinal=0,
        block_text=block,
    )
    view = codec.decode(slice_)
    assert view["settings"]["sample"]["samples"] == "QUJD"

    with pytest.raises(InvalidValue) as excinfo:
        codec.plan_state_writes(
            view, {"paths": {"settings.sample.samples": "AAAA"}}
        )
    assert excinfo.value.detail["reason"] == "derived_leaf_not_writable"
    assert "settings.sample.samples" in excinfo.value.detail["derived_leaves"]

    changes = codec.restore_changes(view, codec.snapshot_document(view))
    assert "settings.sample.samples" not in changes["paths"]
    assert changes["derived_excluded"] == ["settings.sample.samples"]
    # non-vacuity: a writable leaf is still part of the restore
    assert changes["paths"], "the restore set must not be empty for this document"
    assert all(path in changes["paths"] for path in changes["paths"])
    restored = codec.plan_state_writes(view, changes)
    assert restored.units, "the remaining leaves still produce a unit"


def test_codec_refuses_a_corrupt_block_and_an_unrooted_path():
    spec = layout()
    no_json = bytes.fromhex(spec["item_b_prefix_hex"]) + bytes.fromhex(spec["item_b_trailer_hex"])
    with pytest.raises(Exception) as excinfo:
        VC.decode_document(no_json)
    assert type(excinfo.value).__name__ == "Unreadable"

    headerless = "not a vst block\n>"
    with pytest.raises(Exception) as excinfo2:
        VC.parse_block_framing(headerless)
    assert type(excinfo2.value).__name__ == "Unreadable"

    doc = document()
    with pytest.raises(InvalidValue) as excinfo3:
        VC.set_path(doc, "author", "x")
    assert excinfo3.value.detail["roots"] == ["settings"]
    with pytest.raises(InvalidValue):
        VC.set_path(doc, "settings.modulations.99.destination", "x")


def test_codec_route_view_exposes_every_slot_and_the_active_route():
    view = VC.VitalCodec().decode(
        M.TargetedStateSlice(
            chunk_digest="x", line_start=0, line_end=0, header_line="", class_hex=IDENTITY["class_id"],
            ordinal=0, block_text=build_block(document()),
        )
    )
    assert len(view["routes"]) == VC.ROUTE_SLOTS == 64
    active = [row for row in view["routes"] if row["destination"]]
    assert len(active) == 1 and active[0]["slot"] == 1
    assert active[0]["amount"] == -0.25


# ---------------------------------------------------------------------------
# V-11: slice-exact targeting through the provider codec (AUD-001)
# ---------------------------------------------------------------------------


def test_v11_multi_block_targeting_keeps_every_other_block_byte_identical():
    host = vital_host(extra_before=MH.MockFxSlot(
        name="VST3i: Other", module="other.vst3", class_id="11112222333344445555666677778888",
        block=__import__("fixtures").foreign_block_text(), params={"0:9000": 0.1},
    ))
    slice_, ref, facts, chunk = slice_for(host, fx_index=1)
    before_blocks = {block["class_hex"]: block["block_text"] for block in R.parse_blocks(chunk)}
    codec = VC.VitalCodec()
    doc = codec.decode(slice_)
    plan = codec.plan_state_writes(
        doc, {"routes_create": [{"source": "lfo_1", "destination": "filter_1_cutoff", "amount": 0.0625}]}
    )
    new_slice = codec.encode_slice(doc, plan.units[0], slice_)
    spliced = R.splice_block_text(chunk, slice_, new_slice.block_text)
    after_blocks = {block["class_hex"]: block["block_text"] for block in R.parse_blocks(spliced)}
    assert after_blocks["11112222333344445555666677778888"] == before_blocks["11112222333344445555666677778888"]
    assert after_blocks[IDENTITY["class_id"]] != before_blocks[IDENTITY["class_id"]]
    prefix, _suffix = F_split(spliced, chunk, slice_)
    assert prefix == F_split_prefix(chunk, slice_)


def F_split(chunk: str, other: str, slice_):
    lines = chunk.splitlines(keepends=True)
    return "".join(lines[: slice_.line_start]), "".join(lines[slice_.line_end + 1:])


def F_split_prefix(chunk: str, slice_) -> str:
    return F_split(chunk, chunk, slice_)[0]


# ---------------------------------------------------------------------------
# Identity, gates, and the profile
# ---------------------------------------------------------------------------


def test_provider_matches_only_the_measured_vst3_build():
    host = vital_host()
    provider = provider_on_fixtures()
    facts = host.fx_facts(0, 0)
    assert provider.matches(facts) is True
    vst2 = MH.MockFxSlot(name=IDENTITY["fx_name"], module="Vital.vst2", class_id=IDENTITY["class_id"]).facts(
        track_index=0, fx_index=0, track_name="T", fx_count=1
    )
    assert provider.matches(vst2) is False, "a VST2 build must fail closed by not matching"
    other_class = MH.MockFxSlot(name=IDENTITY["fx_name"], module="vital.vst3", class_id="00" * 16).facts(
        track_index=0, fx_index=0, track_name="T", fx_count=1
    )
    assert provider.matches(other_class) is False


def test_version_gate_refuses_an_unmeasured_version_without_an_override():
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    provider.version_gate(profile, None)  # absence tolerated
    for version in SUPPORTED_VERSIONS:
        provider.version_gate(profile, version)
    with pytest.raises(Exception) as excinfo:
        provider.version_gate(profile, "1.6.0")
    assert type(excinfo.value).__name__ == "VersionMismatch"
    assert excinfo.value.detail["override"] is None


def test_profile_from_the_pinned_registry_is_schema_valid_and_cacheable():
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    build = C.build_capability_profile(provider, dict(IDENTITY))
    validation = validate_profile(build.profile)
    assert validation.ok, validation.codes()
    assert build.cacheable, build.validation.codes()
    assert build.source_digest and build.provider_artifact_digest
    assert build.provider_revision == IMPLEMENTATION_REVISION
    assert len(provider.artifact_paths()) == 6

    assert profile["provider"] == "vital"
    assert profile["identity"]["fingerprint"]["param_count"] == 8
    assert profile["identity"]["compatible_versions"]
    assert profile["state"]["codec"] == VC.VITAL_CODEC_NAME
    assert profile["modulation"]["slots"] == 64
    assert profile["modulation"]["write_channel"] == "state"
    assert any("VST2" in limit for limit in profile["limits"])
    assert profile["modulation"]["depth_model"]["accepted"] >= 1
    assert profile["modulation"]["depth_model"]["refused"] >= 4

    controls = {row["id"]: row for row in profile["controls"]}
    assert controls["filter_1_cutoff"]["label"] == "Filter 1 Cutoff"
    assert controls["filter_1_cutoff"]["unit"] == "semitones"
    assert controls["filter_1_cutoff"]["module"] in {row["id"] for row in profile["modules"]}
    enum_controls = [row for row in profile["controls"] if row["type"] == "enum"]
    assert enum_controls and all(len(row["enum_values"]) >= 2 for row in enum_controls)
    assert all(
        row["evidence"]["strength"] in ("strong", "weak", "none") for row in profile["controls"]
    )


# ---------------------------------------------------------------------------
# Registry loader (08 §6.1-§6.3): resolution, validation, no fallback
# ---------------------------------------------------------------------------


def test_loader_reads_the_pinned_directory_and_records_source_hashes():
    registry = load_registry(directory=str(REGISTRY_DIR))
    assert registry.directory == str(REGISTRY_DIR.resolve())
    assert set(registry.files) == {"controls", "modules", "correspondence", "laws", "capabilities"}
    assert all(len(row["sha256"]) == 64 for row in registry.sources)
    assert len(registry.rows()) == 13


def test_loader_refuses_a_missing_directory_naming_the_resolved_path(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(IncompatibleSettings) as excinfo:
        load_registry(directory=str(missing))
    assert excinfo.value.detail["resolved_path"] == str(missing)


def test_configured_directory_never_falls_back_to_the_default(tmp_path, monkeypatch):
    """A broken configured directory is an error, never a quiet default read."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "vital_controls.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REAPER_MCP_VITAL_REGISTRY_DIR", str(broken))
    with pytest.raises(IncompatibleSettings) as excinfo:
        load_registry()
    assert excinfo.value.detail["resolved_path"] == str(broken)
    assert excinfo.value.detail["variable"] == "REAPER_MCP_VITAL_REGISTRY_DIR"
    assert 'vital_checks.json' not in str(excinfo.value.detail)
    assert DEFAULT_REGISTRY_DIR not in excinfo.value.message, (
        "a configured-but-broken directory must never fall back to the default"
    )


def test_empty_variable_value_is_a_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv("REAPER_MCP_VITAL_REGISTRY_DIR", "   ")
    with pytest.raises(IncompatibleSettings) as excinfo:
        load_registry()
    assert "empty value" in excinfo.value.message
    assert excinfo.value.detail["variable"] == "REAPER_MCP_VITAL_REGISTRY_DIR"


def test_expansion_characters_are_refused_rather_than_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("REAPER_MCP_VITAL_REGISTRY_DIR", "~/registry")
    with pytest.raises(IncompatibleSettings) as excinfo:
        load_registry()
    assert "literal path" in excinfo.value.message


def test_invalid_json_and_wrong_shape_are_typed_refusals(tmp_path):
    broken = tmp_path / "registry"
    broken.mkdir()
    good = load_registry(directory=str(REGISTRY_DIR))
    for name, text in good.files.items():
        (broken / Path(text).name).write_text(
            (REGISTRY_DIR / Path(text).name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    target = broken / "vital_modules.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo:
        load_registry(directory=str(broken))
    assert "does not parse" in excinfo.value.message
    target.write_text('{"modules": {}}', encoding="utf-8")
    with pytest.raises(IncompatibleSettings) as excinfo2:
        load_registry(directory=str(broken))
    assert "no non-empty 'modules'" in excinfo2.value.message


# ---------------------------------------------------------------------------
# V-4: depth conversions and refusals match the recorded fit
# ---------------------------------------------------------------------------


def test_v4_recorded_example_and_refusal_set_are_reproduced():
    registry = load_registry(directory=str(REGISTRY_DIR))
    provider = VitalProvider(registry=registry)
    assert provider.convert_depth({"identity": {}}, "filter_1_cutoff", 8) == pytest.approx(0.0625, abs=1e-6)
    fit = D.depth_fit(registry.rows(), registry.scales(), "filter_1_cutoff")
    assert fit.native_per_unit == pytest.approx(128.0, rel=1e-6)
    assert fit.unit == "semitones"

    for destination in ("lfo_1_frequency", "volume", "reverb_decay_time", "osc_1_unison_detune"):
        with pytest.raises(NoConversion) as excinfo:
            provider.convert_depth({"identity": {}}, destination, 8)
        assert "not affine" in excinfo.value.detail["why"], destination
        assert excinfo.value.detail["unit"], destination
    with pytest.raises(NoConversion):
        provider.convert_depth({"identity": {}}, "not_a_destination", 1)


def test_depth_of_zero_says_it_is_a_set_and_a_too_large_depth_refuses():
    registry = load_registry(directory=str(REGISTRY_DIR))
    result = D.modulation_depth(registry.rows(), registry.scales(), "filter_1_cutoff", 0)
    assert result["is_a_set"] is True and result["writes_zero"] is True and result["amount"] == 0.0
    with pytest.raises(InvalidValue) as excinfo:
        D.modulation_depth(registry.rows(), registry.scales(), "filter_1_cutoff", 500)
    assert excinfo.value.detail["max_depth"] == pytest.approx(128.0, rel=1e-6)


# ---------------------------------------------------------------------------
# V-5: rules and preflight refusals
# ---------------------------------------------------------------------------


def test_percentage_vocabulary_is_derived_not_typed():
    registry = load_registry(directory=str(REGISTRY_DIR))
    derived = RULES.percentage_controls_for(registry.scales())
    assert derived, "the pinned scales include a percentage control"
    assert all(str(registry.scales()[name]["states"][-1][1]).strip().endswith("%") for name in derived)


def test_v5_unbound_parameter_ident_refuses_a_control_write_with_a_named_reason():
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    resolved = provider.normalize_request(profile, {"controls": {"filter_1_cutoff": 24}})
    assert resolved.controls == {}
    problem = resolved.problems[0]
    assert problem["code"] == "UnsupportedControl"
    assert problem["detail"]["reason"] == "ident_not_bound"


def test_v5_unknown_route_vocabulary_is_refused_before_any_write():
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    resolved = provider.normalize_request(
        profile, {"routes_create": [{"source": "lfo_99", "destination": "filter_1_cutoff", "depth": 8}]}
    )
    assert resolved.routes_create == []
    assert resolved.problems[0]["code"] == "InvalidRoute"
    assert resolved.problems[0]["detail"]["reason"] == "vocabulary"


def test_unrooted_state_path_and_missing_slot_are_refused():
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    resolved = provider.normalize_request(profile, {"state": {"author": "x"}, "routes_remove": [{}]})
    codes = sorted(problem["code"] for problem in resolved.problems)
    assert codes == ["InvalidRoute", "InvalidValue"]
    assert resolved.state == {}


def test_structural_change_refuses_when_a_disable_target_cannot_be_addressed():
    """The structural DISABLE set must address the real settings, or refuse.

    Measured defect this replaces (2026-10-10): the candidates were spelled as
    document paths verbatim, and the codec *creates* unknown keys - so a
    structural change wrote 13 phantom keys and changed none of the real
    settings, then verified itself against the keys it had just invented (the
    captured live document has 781 settings keys, none containing a space). The
    pinned fixture profile binds none of the 13 candidates, so the honest answer
    is a typed refusal naming the reason - never an invented path.
    """
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    resolved = provider.normalize_request(
        profile, {"controls": {}, "state": {"settings.osc_1_distortion_type": 0}, "structural": True}
    )
    assert resolved.structural is True
    assert not any(
        " " in str(path) for path in resolved.state
    ), "a display-name path is not a document path; it must never be planned"
    reasons = {problem["detail"]["reason"] for problem in resolved.problems}
    assert reasons == {"disable_target_unmapped"}, resolved.problems
    assert len(resolved.problems) == len(RULES.DISABLE_CANDIDATES)
    sample = resolved.problems[0]
    assert sample["code"] == "PlanError"
    assert sample["detail"]["candidate"] in RULES.DISABLE_CANDIDATES


def test_rules_that_the_projected_world_cannot_decide_are_declared_not_guessed():
    rows = RULES.rules_not_evaluated()
    assert {row["rule"] for row in rows} == {
        "fm_needs_modulator",
        "route_into_off_filter",
        "wave_frame_needs_multiframe_wavetable",
    }
    assert all(row["needs"] for row in rows)
    ruleset = provider_on_fixtures_with_profile().dependencies()
    assert {rule["id"] for rule in ruleset.rules} >= {row["rule"] for row in rows}


def provider_on_fixtures_with_profile() -> VitalProvider:
    provider = provider_on_fixtures()
    provider.build_profile(dict(IDENTITY))
    return provider


# ---------------------------------------------------------------------------
# V-3 / V-7: a route write through the P2 engine with the Vital codec (mock host)
# ---------------------------------------------------------------------------


def test_v3_a_route_create_is_refused_by_the_unevaluated_rule_gate():
    """Owner safety-gate correction (2026-10-11).

    This test used to prove a successful two-route write ("one route per state
    write"). That behaviour is no longer reachable through the engine: the rule
    `route_into_off_filter` is declared but not evaluated, its subject (a created
    pair) is decidable and its deciding inputs are not, so **creating a route is
    refused before the first write** rather than applied under an unchecked rule.
    The measured structural limit itself is still proven - one level down, on the
    codec's own plan (see the test below), where no rule gate is involved.
    """
    host = vital_host()
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    facts = host.fx_facts(0, 0)
    registry = TargetTokenRegistry()
    token = registry.issue(facts=facts)
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=IDENTITY
    )
    before = host.slot(0, 0).block
    for dry_run in (True, False):
        result = Transaction(
            provider=provider,
            codec=provider.codec(),
            bridge=host,
            profile=profile,
            ref=ref,
            request={
                "routes_create": [
                    {"source": "lfo_1", "destination": "filter_1_cutoff", "depth": 8},
                    {"source": "env_2", "destination": "filter_1_cutoff", "depth": 12},
                ]
            },
            token=token,
            registry=registry,
            dry_run=dry_run,
        ).run()
        assert result.ok is False, (dry_run, result.data)
        assert isinstance(result.error, DependencyViolation), result.error
        assert "route_into_off_filter" in result.error.detail["unevaluable"]
        assert all(row["evaluated"] is False for row in result.error.detail["findings"])
        assert result.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE
        assert result.outcome.host.write_calls_attempted == 0
        assert result.data.get("would_write") is not True
    assert [row for row in host.log if row[0].startswith("write")] == []
    assert host.slot(0, 0).block == before


def test_v3_one_route_per_state_write_is_the_codec_plan_not_an_engine_rule():
    """The declared structural limit, proven where it lives (the codec).

    Two creates in one request still produce **two** sequential units, each
    carrying exactly one route op - the property the old engine test asserted,
    now proven without a write path so that it stays measurable while the rule
    gate blocks application.
    """
    host = vital_host()
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    _slice, ref, facts, chunk = slice_for(host)
    codec = provider.codec()
    view = codec.decode(host.locate_state_slice(ref, facts, chunk))
    plan = codec.plan_state_writes(
        view,
        {
            "routes_create": [
                {"source": "lfo_1", "destination": "filter_1_cutoff", "amount": 0.0625},
                {"source": "env_2", "destination": "filter_1_cutoff", "amount": 0.09375},
            ]
        },
    )
    assert len(plan.units) == 2, "one route per state write (Vital's declared structural limit)"
    assert all(unit.payload["route_op"]["op"] == "create" for unit in plan.units)
    assert [unit.order_index for unit in plan.units] == [0, 1]

    # and the route write path itself, without the rule gate: apply unit 0 to a
    # copy, then verify it from the decoded read-back (the engine's own rule)
    unit = plan.units[0]
    written = codec.encode_slice(view, unit, host.locate_state_slice(ref, facts, chunk))
    after = codec.decode(written)
    report = codec.verify(
        after, unit, provider.verification_policy()
    )
    assert report.ok, report.rows
    routes = {row["slot"]: row for row in after["routes"] if row["destination"]}
    assert routes[2]["source"] == "lfo_1"
    assert routes[2]["destination"] == "filter_1_cutoff"
    assert routes[2]["amount"] == pytest.approx(0.0625, abs=1e-6)
    # the fixture's own route is untouched (compared, not assumed), and no slot
    # beyond the two involved was written
    before_routes = {row["slot"]: row for row in view["routes"] if row["destination"]}
    untouched = {slot: row for slot, row in before_routes.items() if slot != 2}
    assert {slot: routes[slot] for slot in untouched} == untouched
    assert sorted(routes) == sorted({*untouched, 2})


def test_v7_hard_dependency_refuses_before_any_write_on_a_vital_plan():
    host = vital_host()
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    ruleset = RULES.dependencies(profile)
    findings = ruleset.evaluate(
        {
            "active_routes": 1,
            "routes_create": 1,
            "controls_changed": {},
            "state_paths_changed": {"settings.osc_1_destination": "DIRECT OUT"},
            "n_slots_total": 64,
        },
        ruleset.rules,
    )
    assert findings and findings[0].rule == "direct_out_bypasses_filters"
    assert findings[0].kind == "hard" and findings[0].informational is True


def test_v2_write_order_and_capture_scope_are_the_vital_contract():
    provider = provider_on_fixtures()
    assert provider.write_order() == ("state", "parameters")
    scope = provider.capture_scope()
    assert scope.channels == ["document", "routes", "parameters"]
    assert scope.parameters["scope"] == "full_vector"
    assert scope.restore_order == ["document", "parameters"]
    policy = provider.verification_policy()
    assert policy.tolerances["state.float"] == 1e-5
    assert policy.timing.settle_s == 0.4 and policy.timing.retry_count == 1


def test_p8_a_dry_run_refuses_exactly_what_the_real_run_refuses():
    """P8 regression, found by an independent read-only audit **after** P10.

    The dry run used to build its preview through a path that swallowed a codec
    refusal (`except PluginError: return []`), so a request the write path must
    refuse answered `ok: true, would_write: false` in the preview and a typed
    refusal in the real run. A preview that disagrees with the run it previews is
    an unfaithful preview of exactly the requests that exist to prevent an
    unverifiable write, so both paths are asserted here against the same request.
    """
    doc = document()
    doc["settings"]["sample"] = {"samples": "QUJD"}  # the plugin-owned blob
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [vitals_slot(block=build_block(doc))])]
    )
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    registry = TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=IDENTITY
    )
    request = {"state": {"settings.sample.samples": "AAAA"}}

    def run(dry_run: bool):
        return Transaction(
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

    preview = run(True)
    assert preview.ok is False
    assert preview.error is not None
    assert preview.error.detail["reason"] == "derived_leaf_not_writable"
    assert preview.data.get("would_write") is not True
    assert preview.outcome.verdict is M.Verdict.REFUSED_BEFORE_ANY_WRITE
    assert preview.outcome.host.write_calls_attempted == 0

    real = run(False)
    assert real.ok is False and real.error is not None
    assert real.error.detail["reason"] == "derived_leaf_not_writable"
    assert real.outcome.host.write_calls_attempted == 0
    assert [row for row in host.log if row[0] == "write_state_slice"] == []


def test_p8_dry_run_still_previews_a_writable_request():
    """Non-vacuity for the test above: the preview *can* say yes, and it says it
    with the same plan the real run builds."""
    host = vital_host()
    provider = provider_on_fixtures()
    profile = provider.build_profile(dict(IDENTITY))
    registry = TargetTokenRegistry()
    token = registry.issue(facts=host.fx_facts(0, 0))
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=IDENTITY
    )
    transaction = Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=profile,
        ref=ref,
        request={"state": {"settings.osc_1_filter_1_blend": "Bend"}},
        token=token,
        registry=registry,
        dry_run=True,
    )
    result = transaction.run()
    assert result.ok, result.error
    assert result.data["would_write"] is True
    assert result.outcome.verdict is M.Verdict.NO_WRITES
    assert result.outcome.host.write_calls_attempted == 0
    assert [row for row in host.log if row[0] == "write_state_slice"] == []
