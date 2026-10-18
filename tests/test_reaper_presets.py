"""REAPER's own FX preset store, offline: the measured format, held still.

MEASURED (2026-10-18, live machine, `presets/vst3-Vital.ini`, 4 presets): `[General]` with
`NbPresets=N`, then one `[PresetK]` per preset whose keys are `Data`, `Data_1`, ... (uppercase hex,
32,770 characters = 16,385 bytes per full chunk), `Len` (payload bytes) and `Name`. The decoded
payload reaches the ASCII `VstW` marker at ~offset 48, i.e. it is the FX's VST3 state - the same
bytes a track chunk carries as item A + item B.

These tests are hermetic: no host, no REAPER resource path, no file outside `tmp_path`. The live
channel (`get_fx_presets` / `set_fx_preset` / `save_fx_preset`) is verified through the MCP tools.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.reaper_presets import (  # noqa: E402
    PRESET_CHUNK_BYTES,
    PRESET_PAYLOAD_BYTES,
    append_preset,
    extract_fx_state,
    name_trailer,
    parse_preset_library,
    preset_blob,
    preset_names,
    read_name_trailer,
    render_preset_section,
    split_preset_blob,
    verify_preset_payload,
)


def library_text(*presets: tuple[str, bytes], nb_presets: int | None = None) -> str:
    """A preset file in REAPER's own shape, built by the same renderer the writer uses."""
    lines = ["[General]", f"NbPresets={len(presets) if nb_presets is None else nb_presets}", ""]
    for index, (name, data) in enumerate(presets):
        lines.extend(render_preset_section(index, name, data))
        lines.append("")
    return "\n".join(lines)


def sample_data(size: int = 40, seed: int = 3) -> bytes:
    return bytes(((index * 37 + seed) % 256) for index in range(size))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_the_measured_shape_parses_into_names_sizes_and_a_count():
    text = library_text(("BassHouse", b"x" * 100), ("Bass 808", b"y" * 50))
    parsed = parse_preset_library(text)
    assert parsed["problems"] == [], parsed["problems"]
    assert parsed["nb_presets"] == 2
    assert [preset["name"] for preset in parsed["presets"]] == ["BassHouse", "Bass 808"]
    assert [preset["index"] for preset in parsed["presets"]] == [0, 1]
    assert [preset["len"] for preset in parsed["presets"]] == [100, 50]
    assert preset_names(text) == [
        {"index": 0, "name": "BassHouse", "bytes": 100, "section": "Preset0"},
        {"index": 1, "name": "Bass 808", "bytes": 50, "section": "Preset1"},
    ]


def test_a_payload_that_disagrees_with_len_is_a_problem_not_a_pass():
    """`Len` is the payload size in BYTES; the stored hex is `Len + one checksum byte per chunk`.

    MEASURED on the real file: 11 chunks of a 175,762-byte payload stored 351,546 hex characters =
    2 * (175,762 + 11). The relation is the cheapest consistency probe the format offers.
    """
    text = library_text(("BassHouse", b"x" * 100))
    assert parse_preset_library(text)["problems"] == [], "the renderer's own file must agree"
    broken = text.replace("Len=100", "Len=101")
    problems = parse_preset_library(broken)["problems"]
    assert [row["reason"] for row in problems] == ["payload_size_disagrees_with_len"]
    assert problems[0]["len"] == 101 and problems[0]["chunks"] == 1
    assert problems[0]["expected_chars"] == 2 * (101 + 1)


def test_junk_lines_are_reported_rather_than_skipped():
    text = "[General]\nNbPresets=1\n\nthis is not a key\n[Preset0]\nData=0000\nLen=1\nName=x\n"
    problems = parse_preset_library(text)["problems"]
    assert [row["reason"] for row in problems] == ["not_a_key_value"]


def test_a_preset_without_a_name_is_reported():
    text = "[General]\nNbPresets=1\n\n[Preset0]\nData=0000\nLen=1\n"
    problems = parse_preset_library(text)["problems"]
    assert [row["reason"] for row in problems] == ["preset_without_name"]


# ---------------------------------------------------------------------------
# Writing (the renderer)
# ---------------------------------------------------------------------------


def test_the_renderer_writes_the_measured_chunking():
    """Each chunk is 16,384 payload bytes PLUS REAPER's own checksum byte (sum & 0xFF).

    MEASURED on the real file: the last byte of three consecutive full chunks was `0x87`, `0xcb`,
    `0x19` - the byte-sum of the 16,384 bytes before it in each case. A section written without it
    is a file REAPER did not write, and joining the chunks raw or dropping their FIRST byte breaks
    the plugin document at every boundary (measured: only "drop the last byte" decodes to a
    document with `settings`).
    """
    data = sample_data(PRESET_PAYLOAD_BYTES + 17)
    lines = render_preset_section(2, "BassHouse", data)
    assert lines[0] == "[Preset2]"
    keys = [line.split("=", 1)[0] for line in lines[1:]]
    assert keys == ["Data", "Data_1", "Len", "Name"]
    first, second = (line.split("=", 1)[1] for line in lines[1:3])
    assert len(first) == PRESET_CHUNK_BYTES * 2 == 32_770, "a full chunk is 16,385 bytes of hex"
    assert len(second) == 18 * 2, "16 payload bytes + the checksum"
    part = data[:PRESET_PAYLOAD_BYTES]
    assert first == (part + bytes([sum(part) & 0xFF])).hex().upper(), "uppercase hex, + checksum"
    tail = data[PRESET_PAYLOAD_BYTES:]
    assert second == (tail + bytes([sum(tail) & 0xFF])).hex().upper()
    assert lines[3] == f"Len={len(data)}"
    assert lines[4] == "Name=BassHouse"


def test_the_renderer_refuses_what_it_cannot_write():
    with pytest.raises(ValueError):
        render_preset_section(-1, "x", b"data")
    with pytest.raises(ValueError):
        render_preset_section(0, "   ", b"data")
    with pytest.raises(ValueError):
        render_preset_section(0, "x", b"")


def test_appending_adds_one_section_and_moves_only_the_count():
    first = ("BassHouse", sample_data(64, seed=1))
    second = ("Bass 808", sample_data(PRESET_CHUNK_BYTES + 3, seed=2))
    text = library_text(first, second)
    payload = sample_data(200, seed=9)
    result = append_preset(text, "New Sound", payload)
    assert result["index"] == 2
    assert result["section"] == "Preset2"
    assert result["nb_presets_before"] == 2 and result["nb_presets_after"] == 3
    assert "NbPresets=3" in result["text"]
    # every existing section survives verbatim; only the count line moves
    assert text[text.index("[Preset0]") :].rstrip("\n") in result["text"]
    # and the new section reads back with the payload it was given
    rows = preset_names(result["text"])
    assert [row["name"] for row in rows] == ["BassHouse", "Bass 808", "New Sound"]
    assert rows[2]["bytes"] == len(payload)
    parsed = parse_preset_library(result["text"])
    assert parsed["problems"] == [], parsed["problems"]
    section = [preset for preset in parsed["presets"] if preset["name"] == "New Sound"][0]
    assert section["data_chars"] == 2 * (len(payload) + section["chunks"].__len__())


def test_appending_to_an_empty_or_absent_file_starts_the_library():
    result = append_preset("[General]\nNbPresets=0\n", "First", sample_data(10))
    assert result["index"] == 0 and result["nb_presets_after"] == 1
    assert [row["name"] for row in preset_names(result["text"])] == ["First"]


def test_appending_refuses_a_file_whose_format_is_not_the_measured_one():
    """Better to refuse than to append to something REAPER would then misread."""
    with pytest.raises(ValueError) as bad:
        append_preset("[General]\nNbPresets=x\n[Preset0]\nName=x\n", "New", sample_data(4))
    assert "measured format" in str(bad.value)


def test_appending_refuses_a_file_without_a_count_key():
    with pytest.raises(ValueError) as bad:
        append_preset("[Preset0]\nData=0000\nLen=1\nName=x\n", "New", sample_data(4))
    assert "NbPresets" in str(bad.value)


# ---------------------------------------------------------------------------
# Reading an FX's state out of a track chunk (the payload a preset stores)
# ---------------------------------------------------------------------------


def block(class_id: str, module: str, item_a: bytes, item_b: bytes, name: str = "Synth") -> str:
    """A plugin block in the measured chunk layout (header, item A, item B, terminator)."""
    import base64

    a = base64.b64encode(item_a).decode("ascii")
    b = base64.b64encode(item_b).decode("ascii")
    return "\n".join([f'<VST "{name}" {module} 0 "" {{{class_id}}} ""', a, b, ">"])


CLASS_A = "56535456697461766974616C00000000"
CLASS_B = "11112222333344445555666677778888"


def chunk_with(*blocks: str, extra: str = "") -> str:
    return "<TRACK\nNAME \"Track 1\"\n" + extra + "\n".join(blocks) + "\n>\n"


def test_the_state_is_item_a_followed_by_item_b():
    item_a, item_b = b"a" * 44, b"b" * 300
    chunk = chunk_with(block(CLASS_A, "Vital.vst3", item_a, item_b))
    state = extract_fx_state(chunk, 0)
    assert state["data"] == item_a + item_b
    assert state["item_a_bytes"] == 44 and state["item_b_bytes"] == 300
    assert state["class_hex"] == CLASS_A and state["module"] == "Vital.vst3"
    assert state["matches"] == 1


def test_the_ordinal_picks_among_identical_builds_and_identity_filters_first():
    one = block(CLASS_A, "Vital.vst3", b"1" * 44, b"first")
    two = block(CLASS_A, "Vital.vst3", b"2" * 44, b"second")
    other = block(CLASS_B, "Other.vst3", b"3" * 44, b"other")
    chunk = chunk_with(one, other, two)
    # without an identity the ordinal is the chunk's block order - the track's FX slots
    assert extract_fx_state(chunk, 0)["data"].endswith(b"first")
    assert extract_fx_state(chunk, 1)["data"].endswith(b"other")
    assert extract_fx_state(chunk, 2)["data"].endswith(b"second")
    # with an identity the ordinal counts only the MATCHING blocks
    assert extract_fx_state(chunk, 0, class_hex=CLASS_B)["data"].endswith(b"other")
    assert extract_fx_state(chunk, 0, module="Vital.vst3")["matches"] == 2
    assert extract_fx_state(chunk, 1, module="Vital.vst3")["data"].endswith(b"second")
    # an identity that matches nothing is refused, never resolved to "the only one there"
    with pytest.raises(ValueError):
        extract_fx_state(chunk, 0, module="Absent.vst3")


def test_the_ordinal_outside_the_matches_is_refused():
    chunk = chunk_with(block(CLASS_A, "Vital.vst3", b"1" * 44, b"first"))
    with pytest.raises(ValueError) as bad:
        extract_fx_state(chunk, 1)
    assert "ordinal" in str(bad.value)


def test_a_chunk_without_a_plugin_block_is_refused():
    with pytest.raises(ValueError):
        extract_fx_state("<TRACK\nNAME \"x\"\n>\n", 0)


def test_an_unterminated_block_is_refused():
    chunk = '<TRACK\n<VST "x" m.vst3 0 "" {' + CLASS_A + '} ""\nAAAA\n'
    with pytest.raises(ValueError) as bad:
        extract_fx_state(chunk, 0)
    assert "not terminated" in str(bad.value)


def test_the_checksum_byte_is_what_catches_a_corrupted_chunk():
    """The format carries its own integrity check, and this is it being used."""
    data = sample_data(PRESET_PAYLOAD_BYTES + 32, seed=5)
    text = library_text(("Good", data))
    check = verify_preset_payload(text, 0)
    assert check["found"] is True and check["checksums_ok"] is True
    assert check["len_ok"] is True and check["bad_chunks"] == []
    assert check["payload"] == data, "the reassembled payload is the one that was written"

    # flip one nibble inside the FIRST chunk: the checksum of that chunk must complain
    first = text.splitlines()[4].split("=", 1)[1]
    flipped = ("1" if first[10] != "1" else "2") + first[11:]
    broken = text.replace(first, flipped, 1)
    check = verify_preset_payload(broken, 0)
    assert check["checksums_ok"] is False and check["bad_chunks"] == [0]
    assert check["payload"] is None, "a payload that failed its check is not returned"

    # a `Len` that does not describe the payload is visible too, without decoding
    assert verify_preset_payload(text.replace("Len=%d" % len(data), "Len=7"), 0)["len_ok"] is False


def test_integrity_of_a_section_that_does_not_exist_is_not_a_pass():
    check = verify_preset_payload(library_text(("Only", b"x" * 10)), 7)
    assert check == {"found": False}


def test_multiline_item_b_is_joined_like_the_host_writes_it():
    """A big state is wrapped across lines; the payload is their concatenation."""
    import base64

    item_b = bytes(range(256)) * 20
    encoded = base64.b64encode(item_b).decode("ascii")
    wrapped = "\n".join(encoded[at : at + 128] for at in range(0, len(encoded), 128))
    chunk = chunk_with(f'<VST "x" m.vst3 0 "" {{{CLASS_A}}} ""\nAAAA\n{wrapped}\n>')
    state = extract_fx_state(chunk, 0)
    assert state["data"] == base64.b64decode("AAAA") + item_b


def test_the_state_payload_carries_the_same_wrapper_a_preset_file_does():
    """The save path's premise, checked against the measured preset file.

    MEASURED on `presets/vst3-Vital.ini`: the decoded payload begins with REAPER's own item-A header
    (44 bytes) and the ASCII `VstW` marker sits at offset 48. What `save_fx_preset` stores is the
    payload `extract_fx_state` returns, so that is the shape it must produce - if it did not, a saved
    preset would be bytes REAPER's own file never holds.
    """
    plugins = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
    if plugins not in sys.path:
        sys.path.append(plugins)
    import mock_host as MH
    from reaper_reapy_mcp.plugins import model as M

    import test_p7_vital as T7
    import test_p39_balloon as P39

    document = P39.live_document(
        P39.full_table(frames=1), P39.full_table(frames=1), P39.full_table(frames=1)
    )
    host = MH.MockHostBridge(
        tracks=[MH.MockTrack(0, "Track 1", [T7.vitals_slot(block=T7.build_block(document))])]
    )
    ref = M.PluginInstanceRef(
        host="reaper", location={"track_index": 0, "fx_index": 0}, provider="vital", identity=T7.IDENTITY
    )
    state = extract_fx_state(host.read_state_chunk(ref), 0)
    payload = state["data"]
    assert state["item_a_bytes"] == 44, "REAPER's item A is 44 bytes (measured in the real file)"
    assert payload.find(b"VstW") == 52, (
        "the `VstW` marker lands at 52 in the REAL preset file too (measured on all four presets); "
        "an earlier reading of 48 came from a truncated dump and was wrong",
        payload.find(b"VstW"),
        payload[:64],
    )
    assert payload.find(b"{") == 228, "and the plugin document starts where the real file's does"


# ---------------------------------------------------------------------------
# The name trailer: REAPER writes the preset name INTO the blob
# ---------------------------------------------------------------------------


def blob_for(name: str, core: bytes) -> bytes:
    """A blob in the shape the real file has: the state plus `NUL + name + five NULs`."""
    return preset_blob(core, name)


def test_the_real_file_relation_len_equals_state_plus_name_trailer():
    """The measurement that made the save path change: `Len == 44 + A[0x20] + len(name) + 6`.

    Verified on every preset of `vst3-Vital.ini` (BassHouse 15 = 9 + 6, pad 9 = 3 + 6,
    DirtyBassKongo 20 = 14 + 6, "Bass 808" 14 = 8 + 6), so a blob written without the trailer is
    shorter than anything REAPER itself produced.
    """
    for name in ("BassHouse", "pad", "DirtyBassKongo", "Bass 808", "MCP Check"):
        core = sample_data(100, seed=1)
        blob = preset_blob(core, name)
        assert len(blob) == len(core) + len(name) + 6, name
        assert blob[len(core) :] == b"\x00" + name.encode() + b"\x00" * 5, blob[len(core):]


def test_the_split_finds_the_state_and_the_name_in_a_blob():
    core = b"\x00" * 44 + b"payload bytes"
    # item A declares item B's length at 0x20, so a real core declares it too
    core = bytearray(core)
    core[0x20:0x24] = (len(core) - 44).to_bytes(4, "little")
    core = bytes(core)
    blob = preset_blob(core, "MyPreset")
    split = split_preset_blob(blob)
    assert split["ok"] is True
    assert split["core"] == core
    assert split["artifact"] == b"\x00MyPreset" + b"\x00" * 5
    assert split["name"] == "MyPreset"
    assert split["declared_b"] == len(core) - 44
    # re-saving under a new name replaces the name and keeps the state byte-for-byte
    again = split_preset_blob(preset_blob(split["core"], "Other"))
    assert again["name"] == "Other"
    assert again["core"] == split["core"]


def test_a_blob_without_the_measured_shape_is_not_decoded_as_a_name():
    core = bytearray(b"\x00" * 44 + b"state")
    core[0x20:0x24] = len(b"state").to_bytes(4, "little")
    core = bytes(core)
    not_a_name = core + b"\x01garbage\x00\x00\x00\x00\x00"
    split = split_preset_blob(not_a_name)
    assert split["ok"] is True and split["artifact"] == b"\x01garbage\x00\x00\x00\x00\x00"
    assert split["name"] is None, "a region that is not the measured shape is 'not understood'"
    assert read_name_trailer(b"") is None
    assert read_name_trailer(b"\x00ab") is None, "too short to be the shape"


def test_an_unusable_length_field_reports_the_whole_payload_as_the_core():
    """Nothing is silently truncated: `ok: false` and the payload is the core."""
    split = split_preset_blob(b"tiny")  # shorter than the header itself
    assert split["ok"] is False and split["core"] == b"tiny" and split["name"] is None
    long_enough = bytearray(b"\x00" * 44)
    long_enough[0x20:0x24] = (10_000).to_bytes(4, "little")  # declares more than exists
    split = split_preset_blob(bytes(long_enough) + b"tail")
    assert split["ok"] is False and split["artifact"] == b"" and split["name"] is None


def test_an_empty_name_cannot_be_written():
    with pytest.raises(ValueError):
        name_trailer("")


# ---------------------------------------------------------------------------
# The controller and the tools that expose it (import safety, no host)
# ---------------------------------------------------------------------------


def test_the_preset_controller_and_its_four_tools_load_without_a_host():
    """Importing the channel must not connect to REAPER: the server registers tools at startup."""
    import importlib

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    module = importlib.import_module("controllers.preset_controller")
    for method in ("get_fx_presets", "set_fx_preset", "navigate_fx_presets", "save_fx_preset"):
        assert callable(getattr(module.PresetController, method)), method
    combined = importlib.import_module("reaper_controller")
    assert "PresetController" in [base.__name__ for base in combined.ReaperController.__mro__]
    tools = importlib.import_module("mcp_tools")
    assert callable(tools.setup_mcp_tools)
