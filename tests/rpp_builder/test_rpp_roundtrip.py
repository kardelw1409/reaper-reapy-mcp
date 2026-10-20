"""F1 acceptance: `render(parse(x)) == x`, byte for byte, on every reference.

Reference paths and digests are the ones recorded in `Docs/RPP_FORMAT.md`
(section 0 for `aligned_v4`, section 9.0 for the six F0-B files). A digest that
does not match is refused by name, because a changed reference is a DIFFERENT
file, not a formatting difference - accepting it would quietly move the goal.

Paths are overridable (`RPP_REF_DIR`, `RPP_ALIGNED_V4`) and a missing reference
SKIPS with the reason, so the suite still runs on a machine without the audio
work tree.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from reaper_reapy_mcp.rpp_builder import parse, render, render_bytes, unquote

REF_DIR = Path(os.environ.get("RPP_REF_DIR", r"C:\REAPER\work\refs\rpp_format"))
ALIGNED_V4 = Path(
    os.environ.get(
        "RPP_ALIGNED_V4",
        r"C:\REAPER\work\takes\dnb_v1\aligned_v4\project\dnb-neurofunk-174.rpp",
    )
)

# RPP_FORMAT.md section 9.0, in the order the document lists them.
REFERENCE_DIGESTS: tuple[tuple[str, str], ...] = (
    ("F0B_folder_send_fx_env.rpp", "8691404978ac2a0d447d095984cdbf6e7b826617c7000ad105bdecc53bbe2ba7"),
    ("F0B2_folder_boundary.rpp", "f985efc797c9a85f68c75617f1323ea23dc574b07e891980bcb9c4cc34a61e7c"),
    ("F0B3_nested_folders.rpp", "5123392899cbe2113ba84cba3eade5f5f5a8f3338de301c80d368e8d541ea42e"),
    ("F0B4_nested_two_closers.rpp", "c66d44b18a70f7a6d6c1f210cf53576a42212942005a950ba8e8db5ffebdc1c0"),
    ("F0B5_compact_zero.rpp", "2603f1776d231f0606f27ee9d81fc14430ef7794c86172942d5f3ef4388918fb"),
    ("F0B6_compact_two.rpp", "186fa3d5c97f554abc3144f7ea8a71edf7d99faebbc4fd16dbda3c355e10359f"),
)

# RPP_FORMAT.md section 0.
ALIGNED_DIGEST = "a23d252becc824e97b720ad83533b0013204ffa5d9de08e1aade3de389a6eff3"

# RPP_FORMAT.md section 4: the 14 track names, in file order.
EXPECTED_TRACK_NAMES = [
    "AI_MASTER",
    "AI_VOCAL",
    "AI_INSTR",
    "MIDI_VOCAL",
    "MIDI_INS",
    "AI_DRUMS",
    "AI_BASS",
    "AI_OTHER",
    "MIDI_BASS",
    "MIDI_DRUMS_KICK",
    "MIDI_DRUMS_SNARE",
    "MIDI_DRUMS_HIHAT",
    "MIDI_DRUMS_TOM",
    "MIDI_DRUMS_CYMBAL",
]


def _cases() -> list:
    """One parametrized case per reference file, with its recorded digest."""
    cases = [
        pytest.param(REF_DIR / name, digest, id=name)
        for name, digest in REFERENCE_DIGESTS
    ]
    cases.append(pytest.param(ALIGNED_V4, ALIGNED_DIGEST, id=ALIGNED_V4.name))
    return cases


def _load(path: Path, digest: str) -> bytes:
    """Read a reference and refuse it if it is not the recorded file."""
    if not path.exists():
        pytest.skip("reference not on this machine: %s (set RPP_REF_DIR / RPP_ALIGNED_V4)" % path)
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        pytest.fail(
            "reference changed on disk - refusing to compare\n"
            "  file:            %s\n"
            "  expected sha256: %s\n"
            "  actual sha256:   %s\n"
            "  %d bytes\n"
            "If this is a deliberate new reference, update Docs/RPP_FORMAT.md and "
            "REFERENCE_DIGESTS together." % (path, digest, actual, len(data))
        )
    return data


def _first_difference(expected: bytes, actual: bytes) -> str:
    """Point at the first differing byte, with its line number and both lines."""
    limit = min(len(expected), len(actual))
    offset = limit
    for index in range(limit):
        if expected[index] != actual[index]:
            offset = index
            break
    line_number = expected.count(b"\n", 0, offset) + 1
    expected_start = expected.rfind(b"\n", 0, offset) + 1
    actual_start = actual.rfind(b"\n", 0, offset) + 1
    expected_end = expected.find(b"\n", offset)
    actual_end = actual.find(b"\n", offset)
    if expected_end < 0:
        expected_end = len(expected)
    if actual_end < 0:
        actual_end = len(actual)
    return (
        "first difference at byte %d (line %d)\n"
        "  original: %r\n"
        "  rendered: %r\n"
        "  sizes:    %d vs %d bytes"
        % (
            offset,
            line_number,
            expected[expected_start:expected_end],
            actual[actual_start:actual_end],
            len(expected),
            len(actual),
        )
    )


# --- level 2 acceptance -----------------------------------------------------


@pytest.mark.parametrize("path, digest", _cases())
def test_roundtrip_is_byte_identical(path: Path, digest: str):
    """The F1 criterion: nothing is lost and nothing is invented."""
    data = _load(path, digest)
    written = render_bytes(parse(data.decode("utf-8")))
    assert written == data, _first_difference(data, written)


@pytest.mark.parametrize("path, digest", _cases())
def test_parse_is_idempotent(path: Path, digest: str):
    """`parse(render(parse(x)))` yields the same tree, not merely the same text."""
    data = _load(path, digest)
    first = parse(data.decode("utf-8"))
    second = parse(render(first))
    assert second == first


@pytest.mark.parametrize("path, digest", _cases())
def test_line_terminators_are_preserved(path: Path, digest: str):
    """CRLF count is unchanged (RPP_FORMAT.md section 10, item 2)."""
    data = _load(path, digest)
    text = render(parse(data.decode("utf-8")))
    assert text.count("\r\n") == data.count(b"\r\n")
    assert text.count("\n") == data.count(b"\n")


# --- the file's own measured structure --------------------------------------


@pytest.fixture(scope="module")
def aligned_root():
    """The parsed `aligned_v4` project block, or a skip if the reference is absent."""
    if not ALIGNED_V4.exists():
        pytest.skip("aligned_v4 reference is not on this machine: %s" % ALIGNED_V4)
    data = ALIGNED_V4.read_bytes()
    assert hashlib.sha256(data).hexdigest() == ALIGNED_DIGEST
    root = parse(data.decode("utf-8")).root
    assert root is not None
    return root


def test_project_block_is_reaper_project(aligned_root):
    assert aligned_root.tag == "REAPER_PROJECT"
    # RPP_FORMAT.md section 2: `<REAPER_PROJECT 0.1 "7.53/win64" <n> 0`.
    assert aligned_root.args[0] == "0.1"
    assert aligned_root.args[1] == '"7.53/win64"'


def test_counts_match_the_measured_structure(aligned_root):
    """Numbers taken from RPP_FORMAT.md, not from this parser's own output."""
    blocks = list(aligned_root.walk())
    tracks = [node for node in blocks if node.tag == "TRACK"]
    items = [node for node in blocks if node.tag == "ITEM"]
    sources = [node for node in blocks if node.tag == "SOURCE"]
    wave = [node for node in sources if node.args[:1] == ("WAVE",)]
    midi = [node for node in sources if node.args[:1] == ("MIDI",)]

    # Section 4: 14 flat tracks, 57 items. Section 7: 6 WAVE + 51 MIDI sources.
    assert len(tracks) == 14
    assert len(items) == 57
    assert (len(wave), len(midi)) == (6, 51)

    # Section 7.2: 4405 event lines in the whole file.
    events = [field for node in blocks for field in node.fields() if field.key == "E"]
    assert len(events) == 4405

    # Section 6: 24 `SM` lines holding 846 triples (line wrapping is free).
    stretch = [field for node in blocks for field in node.fields() if field.key == "SM"]
    assert len(stretch) == 24
    assert sum(len(field.args) // 3 for field in stretch) == 846

    # Section 3: 8 regions (pairs) + 1 plain marker = 17 MARKER lines.
    markers = [field for field in aligned_root.fields() if field.key == "MARKER"]
    assert len(markers) == 17


def test_track_guid_is_repeated_in_trackid(aligned_root):
    """RPP_FORMAT.md section 10, item 6: TRACKID equals the GUID of the tag line."""
    for track in aligned_root.find_all("TRACK"):
        assert track.args[0] == track.field_value("TRACKID")


def test_track_names_need_depth_and_a_flat_scan_gets_item_names(aligned_root):
    """The trap of section 1: `NAME` exists at track AND item level.

    Depth-aware reading gives the 14 track names. A depth-blind scan - "the last
    NAME inside this track wins" - returns item names instead, and that result
    looks perfectly plausible.
    """
    tracks = aligned_root.find_all("TRACK")
    assert len(tracks) == 14

    own_names = [track.field_value("NAME") for track in tracks]
    assert own_names == EXPECTED_TRACK_NAMES

    flat_names = []
    for track in tracks:
        names = [
            field.value
            for node in track.walk()
            for field in node.fields()
            if field.key == "NAME"
        ]
        flat_names.append(names[-1] if names else "")

    changed = sum(1 for own, flat in zip(own_names, flat_names) if own != flat)
    assert changed == len(EXPECTED_TRACK_NAMES), (
        "the depth trap is not exercised: only %d of %d tracks would be renamed by a "
        "flat scan (flat names: %r)" % (changed, len(own_names), flat_names)
    )
    # And the names a flat scan returns are file names, exactly as the document says.
    # One of them is QUOTED (`"Ins - ins.mid"`), which is the format's rule for a value
    # containing a space (section 8) - the quotes are part of the token on this layer.
    assert all(unquote(flat).endswith((".wav", ".mid")) for flat in flat_names), flat_names
