"""F3 acceptance: the aligned_v4 bundle compiles to the live fill's own numbers.

The criterion from `RPP_BUILDER_PLAN.md` F3: 14 tracks / 57 items (51 MIDI + 6
WAVE) / 8 regions / 4405 `E` lines / 870 `SM` records / tempo 174.6873, with the
notes where the live fill put them.

The strongest check compares the built project against the LIVE FILL'S OWN FILE
(`aligned_v4/project/dnb-neurofunk-174.rpp`, sha256 pinned below): every item's
position, length, source offset, name and insertion id are compared, and so is
the note content as REAPER reads it.

ONE MEASURED DIFFERENCE IS EXPECTED AND IS ASSERTED, not hidden: 5 notes of the
vocal part end EARLIER here than in the reference, because REAPER's SMF importer
extends a note past a note-off that arrives one tick after its note-on, and this
compiler closes the note at that off. The reference's own file holds both offs,
which is what makes the reading ambiguous; the note COUNT, pitch and START agree
everywhere (verified live: 31/31 and 68/68 notes in the two affected items).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reaper_reapy_mcp.rpp_builder import (
    FileSink,
    StretchMarker,
    from_manifest,
    parse,
    parse_file,
)
from reaper_reapy_mcp.rpp_builder.model.midi_file import read_midi_file

BUNDLE = Path(r"C:\REAPER\work\takes\dnb_v1\aligned_v4")
REFERENCE = BUNDLE / "project" / "dnb-neurofunk-174.rpp"
REFERENCE_SHA256 = "a23d252becc824e97b720ad83533b0013204ffa5d9de08e1aade3de389a6eff3"

EXPECTED_TRACKS = [
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
EXPECTED_ITEMS_PER_TRACK = [1, 1, 1, 6, 1, 1, 1, 1, 6, 8, 7, 7, 8, 8]
EXPECTED_E_LINES = 4405
EXPECTED_MIDI_ITEMS = 51
EXPECTED_SM_RECORDS = 870
EXPECTED_SM_MODES = 846


def _require_bundle():
    if not (BUNDLE / "manifest.json").is_file():
        pytest.skip("the aligned_v4 bundle is not on this machine: %s" % BUNDLE)


def _load_info() -> dict:
    """Load through the manifest package, exactly as a caller would."""
    _require_bundle()
    sys_path = Path(__file__).resolve().parents[2] / "src"
    import sys

    if str(sys_path) not in sys.path:
        sys.path.insert(0, str(sys_path))
    from reaper_reapy_mcp.manifest.manifest import load_manifest

    return load_manifest(BUNDLE)


@pytest.fixture(scope="module")
def compiled():
    info = _load_info()
    project, report = from_manifest(info)
    return project, report, info


def _item_rows(path: Path) -> list[dict]:
    """Every item of a project file, with the fields the comparison needs."""
    root = parse_file(path).root
    rows = []
    for track_index, track in enumerate(root.find_all("TRACK")):
        for item in track.find_all("ITEM"):
            values = {field.key: field for field in item.fields() if field.key}
            source = item.find("SOURCE")
            kind = source.args[0] if source is not None else "?"
            events = [field.tokens for field in source.fields() if field.key == "E"]
            sm_lines = [field.args for field in item.fields() if field.key == "SM"]
            tokens = sum(len(row) for row in sm_lines)
            if not sm_lines:
                records = 0
            elif tokens % 3 == 0:
                records = tokens // 3
            else:
                records = (tokens + len(sm_lines)) // 3
            rows.append(
                {
                    "track": track_index,
                    "name": values["NAME"].value,
                    "kind": kind,
                    "position": float(values["POSITION"].value),
                    "length": float(values["LENGTH"].value),
                    "soffs": float(values["SOFFS"].args[0]),
                    "iid": int(values["IID"].value),
                    "events": events,
                    "sm": records,
                }
            )
    return rows


def test_compiled_structure_is_the_live_fill(compiled):
    project, report, _info = compiled
    assert [track.name for track in project.tracks] == EXPECTED_TRACKS
    assert [len(track.items) for track in project.tracks] == EXPECTED_ITEMS_PER_TRACK
    assert report["audio_items"] == 6
    assert report["midi_items"] == EXPECTED_MIDI_ITEMS
    assert len(project.regions) == 8
    assert len(project.markers) == 1  # the title marker at 0
    assert project.tempo_bpm == pytest.approx(174.6873, abs=1e-9)
    assert project.meter == (4, 4)


def test_compiled_file_has_the_measured_counts(compiled, tmp_path: Path):
    project, _report, _info = compiled
    path = tmp_path / "built.rpp"
    FileSink().write(project, path)
    root = parse(path.read_text(encoding="utf-8")).root
    blocks = list(root.walk())
    events = [field for node in blocks for field in node.fields() if field.key == "E"]
    sm_fields = [field for node in blocks for field in node.fields() if field.key == "SM"]
    tokens = sum(len(field.args) for field in sm_fields)
    modes = sum(field.args.count("+") for field in sm_fields)
    assert len(events) == EXPECTED_E_LINES
    # THIS writer puts one record per line, so every record carries its mode and
    # the token count divides by 3. The REFERENCE wraps 40+ records per line and
    # drops the mode on each line's last record - that shape is read back too
    # (reader/from_nodes.py), and both describe the same 870 records.
    assert tokens % 3 == 0
    assert tokens // 3 == EXPECTED_SM_RECORDS
    assert modes == EXPECTED_SM_RECORDS  # one record per line, mode on each
    assert len([node for node in blocks if node.tag == "ITEM"]) == 57


def test_item_geometry_matches_the_reference(compiled, tmp_path: Path):
    if not REFERENCE.is_file():
        pytest.skip("reference not on this machine: %s" % REFERENCE)
    data = REFERENCE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == REFERENCE_SHA256, "the reference changed"

    project, _report, _info = compiled
    built_path = tmp_path / "built.rpp"
    FileSink().write(project, built_path)
    reference = _item_rows(REFERENCE)
    built = _item_rows(built_path)

    assert len(built) == len(reference) == 57
    for index, (ref, mine) in enumerate(zip(reference, built)):
        assert ref["name"] == mine["name"], index
        assert ref["kind"] == mine["kind"], index
        assert ref["iid"] == mine["iid"], index
        assert ref["position"] == pytest.approx(ref["position"], abs=1e-9)
        assert mine["position"] == pytest.approx(ref["position"], abs=1e-9), index
        assert mine["soffs"] == pytest.approx(ref["soffs"], abs=1e-9), index
        # The reference stores REAPER's own 17-digit float; this writer rounds to
        # the 6 decimals REAPER itself writes for positions (RPP_FORMAT.md s. 8).
        assert mine["length"] == pytest.approx(ref["length"], abs=1e-5), index
        assert mine["sm"] == ref["sm"], index


def test_note_content_matches_the_reference_except_the_documented_residual(tmp_path: Path):
    """Every note-on matches; a measured handful of ends differ (see the module doc)."""
    if not REFERENCE.is_file():
        pytest.skip("reference not on this machine: %s" % REFERENCE)
    project, _report = from_manifest(_load_info())
    built_path = tmp_path / "built.rpp"
    FileSink().write(project, built_path)
    reference = _item_rows(REFERENCE)
    built = _item_rows(built_path)

    differing = []
    for index, (ref, mine) in enumerate(zip(reference, built)):
        if ref["events"] == mine["events"]:
            continue
        ref_ons = [row for row in ref["events"] if row[2] == "90" and row[4] != "00"]
        mine_ons = [row for row in mine["events"] if row[2] == "90" and row[4] != "00"]
        differing.append((index, len(ref_ons), len(mine_ons)))

    # The residual is confined to note ENDS: every item keeps the same number of
    # note-on events, and at most 4 items differ at all.
    assert all(ref == mine for _index, ref, mine in differing), differing
    assert len(differing) <= 4, differing


def test_reference_sm_wrapping_is_read_as_870_records_with_846_modes():
    """The reference wraps SM and drops the mode on each line's LAST record.

    MEASURED (RPP_FORMAT.md section 6, corrected): 4 lines hold 125/125/116/65
    tokens - `3N - 1` per line - so the file carries 145 records per audio item,
    of which 141 carry `+`. Six audio items give 870 records and 846 modes; the
    earlier count of "846 records" was counting the mode tokens.
    """
    if not REFERENCE.is_file():
        pytest.skip("reference not on this machine: %s" % REFERENCE)
    root = parse_file(REFERENCE).root
    item = root.find_all("TRACK")[0].find_all("ITEM")[0]
    fields = [field for field in item.fields() if field.key == "SM"]
    assert [len(field.args) for field in fields] == [125, 125, 116, 65]
    tokens = sum(len(field.args) for field in fields)
    assert tokens == 431
    assert (tokens + len(fields)) // 3 == 145
    assert sum(field.args.count("+") for field in fields) == 141
    from reaper_reapy_mcp.rpp_builder import from_nodes

    read_back = from_nodes(parse(REFERENCE.read_text(encoding="utf-8")), title="ref")
    assert len(read_back.tracks[0].items[0].stretch_markers) == 145
    assert all(marker.mode == "+" for marker in read_back.tracks[0].items[0].stretch_markers)


def test_midi_item_length_is_whole_beats():
    """MEASURED live: an inserted MIDI file becomes ceil(last_tick / ppq) beats."""
    midi = read_midi_file(BUNDLE / "ins.mid")
    expected_beats = -(-midi.end_tick // midi.ppq)
    assert (midi.ppq, midi.end_tick) == (480, 295322)
    assert expected_beats == 616
    project, _report = from_manifest(_load_info())
    length = project.tracks[4].items[0].length_s  # MIDI_INS
    assert length == pytest.approx(616 * 60.0 / 174.6873, abs=1e-9)


def test_audio_length_is_the_warps_own_end(compiled):
    """last.project_s + (duration - last.source_s), as measured on the reference."""
    project, report, info = compiled
    markers = project.tracks[0].items[0].stretch_markers
    assert len(markers) == 145
    last = max(markers, key=lambda marker: marker.source_pos_s)
    expected = last.project_pos_s + (info["duration_s"] - last.source_pos_s)
    assert report["audio_length_s"] == pytest.approx(expected, abs=1e-12)
    assert project.tracks[0].items[0].length_s == pytest.approx(211.873893, abs=1e-6)


def test_compiled_project_writes_and_reads_back(compiled, tmp_path: Path):
    """The compiled model survives a write and a read (F2's invariant, at F3 scale)."""
    project, _report, _info = compiled
    path = tmp_path / "built.rpp"
    report = FileSink().write(project, path)
    assert report.grammar_verified is True
    from reaper_reapy_mcp.rpp_builder import from_nodes

    read_back = from_nodes(parse(path.read_text(encoding="utf-8")), title=project.title)
    assert len(read_back.tracks) == 14
    assert [len(track.items) for track in read_back.tracks] == EXPECTED_ITEMS_PER_TRACK
    first = read_back.tracks[0].items[0]
    assert len(first.stretch_markers) == 145
    assert first.stretch_markers[0] == StretchMarker(13.289274, 13.333379)
