"""F2 acceptance: model -> file -> model, and the measured invariants.

The strongest check here is `write -> read -> write == write`: it proves the
reader recovers everything the writer wrote, without having to trust a field by
field comparison. The semantic comparison additionally covers values, ignoring
only the GUIDs the writer generates when the model leaves them unset.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from reaper_reapy_mcp.rpp_builder import (
    AudioSource,
    FileSink,
    Item,
    Marker,
    MidiEvent,
    MidiSource,
    Project,
    Region,
    Send,
    StretchMarker,
    Track,
    ValidationError,
    from_nodes,
    parse,
    render_bytes,
    to_nodes,
    validate,
)
from reaper_reapy_mcp.rpp_builder.model.units import (
    format_exact,
    format_number,
    quote_text,
)

REFERENCE = Path(
    r"C:\REAPER\work\takes\dnb_v1\aligned_v4\project\dnb-neurofunk-174.rpp"
)
REFERENCE_SHA256 = "a23d252becc824e97b720ad83533b0013204ffa5d9de08e1aade3de389a6eff3"

_GENERATED_GUIDS = {"guid", "take_guid", "pooled_events_guid"}
#: Fields the FILE cannot carry back: `iid` is read from the item it belongs to
#: (so it survives as a value) and `end_tick` describes the source file's length,
#: which only the compiler knows.
_NOT_ROUNDTRIPPED = {"end_tick", "iid"}


def _demo() -> Project:
    events = (
        MidiEvent(0, 0x90, 36, 100),
        MidiEvent(240, 0x80, 36, 0),
        MidiEvent(480, 0x90, 38, 90),
        MidiEvent(720, 0x80, 38, 0),
    )
    audio = Item(
        position_s=1.5,
        length_s=10.25,
        source=AudioSource(file_path=r"C:\Audio\song.wav"),
        source_offset_s=0.047206,
        name="song.wav",
        stretch_markers=(
            StretchMarker(1.5, 1.5),
            StretchMarker(4.125, 4.13),
        ),
    )
    midi = Item(
        position_s=12.0,
        length_s=8.0,
        source=MidiSource(ppq=480, events=events),
        source_offset_s=23.35602,
        name="vocal.mid",
    )
    return Project(
        title="demo",
        tempo_bpm=174.6873,
        meter=(4, 4),
        tracks=(
            Track(name="AI_MASTER", items=(audio,)),
            Track(name="MIDI_VOCAL", items=(midi,)),
        ),
        markers=(Marker("intro", 0.0),),
        regions=(Region("verse", 21.982136, 45.338156),),
    )


def _strip_generated(value):
    """Drop GUID fields the writer fills in, so two models can be compared."""
    if dataclasses.is_dataclass(value):
        return {
            field.name: _strip_generated(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name not in _GENERATED_GUIDS and field.name not in _NOT_ROUNDTRIPPED
        }
    if isinstance(value, tuple):
        return tuple(_strip_generated(item) for item in value)
    return value


def test_model_write_read_write_is_byte_identical(tmp_path: Path):
    first = tmp_path / "demo.rpp"
    FileSink().write(_demo(), first)
    text = first.read_text(encoding="utf-8")
    read_back = from_nodes(parse(text), title="demo")
    second = render_bytes(to_nodes(read_back))
    assert second == first.read_bytes(), "read -> write changed the project"


def test_model_roundtrip_keeps_every_value_except_generated_guids(tmp_path: Path):
    path = tmp_path / "demo.rpp"
    FileSink().write(_demo(), path)
    read_back = from_nodes(parse(path.read_text(encoding="utf-8")), title="demo")
    assert _strip_generated(read_back) == _strip_generated(_demo())


def test_two_writes_are_byte_identical(tmp_path: Path):
    """Decision 4: two runs produce one file, so builds and diffs are stable."""
    first = tmp_path / "one.rpp"
    second = tmp_path / "two.rpp"
    FileSink().write(_demo(), first)
    FileSink().write(_demo(), second)
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()


def test_midi_soffs_second_value_is_the_measured_double(tmp_path: Path):
    path = tmp_path / "demo.rpp"
    FileSink().write(_demo(), path)
    text = path.read_text(encoding="utf-8")
    # Measured: 23.35602 * (174.6873 / 60) == 68.00000120909999, while the
    # algebraically equal 23.35602 * 174.6873 / 60 == 68.0000012091.
    assert "SOFFS 23.35602 68.00000120909999" in text


def test_midi_events_are_written_as_deltas(tmp_path: Path):
    path = tmp_path / "demo.rpp"
    FileSink().write(_demo(), path)
    root = parse(path.read_text(encoding="utf-8")).root
    source = root.find_all("TRACK")[1].find_all("ITEM")[0].find("SOURCE")
    events = [field.tokens for field in source.fields() if field.key == "E"]
    assert events[0] == ("E", "0", "90", "24", "64")
    assert events[1] == ("E", "240", "80", "24", "00")
    deltas = [int(tokens[1]) for tokens in events]
    # Four model events plus the measured closing CC123, placed at the source end
    # rounded UP to whole beats: 720 ticks -> 960 (2 beats) -> delta 240.
    assert deltas == [0, 240, 240, 240, 240]
    assert sum(deltas) == 960
    assert events[-1] == ("E", "240", "b0", "7b", "00")


def test_project_header_constants_are_the_reference_lines(tmp_path: Path):
    """Every measured header line is emitted verbatim (except volatile ones).

    The reference is refused if its digest changed: comparing against a
    different file would move the goal silently.
    """
    if not REFERENCE.exists():
        pytest.skip("reference not on this machine: %s" % REFERENCE)
    data = REFERENCE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == REFERENCE_SHA256

    path = tmp_path / "demo.rpp"
    FileSink().write(_demo(), path)
    built = {
        line.strip()
        for line in path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    }
    reference_lines = data.decode("utf-8").replace("\r\n", "\n").split("\n")
    compared = 0
    for line in reference_lines[1:100]:  # lines 2..100: NOTES through the PT line
        stripped = line.strip()
        if not stripped or "EGUID" in stripped or stripped.startswith("CURSOR"):
            continue
        assert stripped in built, "header line not reproduced: %r" % stripped
        compared += 1
    assert compared >= 90, compared


def test_region_ids_do_not_collide_with_marker_ids(tmp_path: Path):
    """Ids are unique across markers and regions.

    MEASURED LIVE: REAPER accepts the reference's reuse of id 1 (the region still
    read back as 0.0..2.0), so this is a client-safety choice, not a REAPER rule.
    """
    path = tmp_path / "ids.rpp"
    FileSink().write(_demo(), path)
    root = parse(path.read_text(encoding="utf-8")).root
    markers = [field.tokens for field in root.fields() if field.key == "MARKER"]
    assert markers[0][1] == "1"  # the plain marker
    assert markers[1][1] == "2"  # the region opens on the next id
    assert markers[2][1] == "2"  # and closes on the same one


def test_unknown_item_field_survives(tmp_path: Path):
    item = Item(
        position_s=1.0,
        length_s=2.0,
        source=AudioSource(file_path=r"C:\Audio\a.wav"),
        name="a.wav",
        extra=(("SOMEFUTUREFIELD", ("1", '"x"')),),
    )
    project = Project(
        title="extra", tempo_bpm=120.0, tracks=(Track(name="T", items=(item,)),)
    )
    path = tmp_path / "extra.rpp"
    FileSink().write(project, path)
    text = path.read_text(encoding="utf-8")
    assert "SOMEFUTUREFIELD 1 \"x\"" in text
    read_back = from_nodes(parse(text), title="extra")
    assert read_back.tracks[0].items[0].extra == (("SOMEFUTUREFIELD", ("1", '"x"')),)


def test_unknown_midi_block_survives_verbatim(tmp_path: Path):
    """The measured `<X ...>` blob is carried as lines, not parsed."""
    x_block = (
        "        <X 0 0 0 0 3 Ins",
        "          /wNJbnM=",
        "        >",
    )
    midi = Item(
        position_s=0.0,
        length_s=4.0,
        source=MidiSource(
            ppq=480,
            events=(MidiEvent(0, 0x90, 60, 100), MidiEvent(480, 0x80, 60, 0)),
            extra_raw=x_block,
        ),
        name="x.mid",
    )
    project = Project(
        title="blob", tempo_bpm=120.0, tracks=(Track(name="M", items=(midi,)),)
    )
    first = tmp_path / "blob.rpp"
    FileSink().write(project, first)
    text = first.read_text(encoding="utf-8")
    assert "\n".join(x_block) in text
    read_back = from_nodes(parse(text), title="blob")
    assert read_back.tracks[0].items[0].source.extra_raw == x_block
    assert render_bytes(to_nodes(read_back)) == first.read_bytes()


def test_number_and_text_formatting_match_measured_style():
    # RPP_FORMAT.md section 8.
    assert format_number(25.12058) == "25.12058"
    assert format_number(13.289274) == "13.289274"
    assert format_number(0.0) == "0"
    assert format_number(-0.0) == "0"
    assert format_number(174.6873) == "174.6873"
    assert format_exact(68.00000120909999) == "68.00000120909999"
    assert quote_text("AI_MASTER") == "AI_MASTER"
    assert quote_text("Ins - ins.mid") == '"Ins - ins.mid"'
    assert quote_text("") == '""'


def test_file_sink_reports_and_leaves_no_temporary(tmp_path: Path):
    path = tmp_path / "demo.rpp"
    report = FileSink().write(_demo(), path)
    assert report.grammar_verified is True
    assert report.bytes == path.stat().st_size
    assert report.lines == path.read_bytes().count(b"\n")
    assert report.elapsed_ms >= 0.0
    assert list(tmp_path.glob("*.tmp")) == []
    assert path.read_bytes().endswith(b">\r\n")


# --- what the validator must refuse, and that it writes nothing -------------


def _invalid_cases() -> list[tuple[str, Project]]:
    base = dict(title="bad", tempo_bpm=120.0)
    good_item = Item(
        position_s=0.0,
        length_s=1.0,
        source=AudioSource(file_path=r"C:\a.wav"),
        name="a.wav",
    )
    cases: list[tuple[str, Project]] = [
        (
            "fade",
            Project(
                **base,
                tracks=(
                    Track(
                        name="T",
                        items=(dataclasses.replace(good_item, fade_in_s=0.1),),
                    ),
                ),
            ),
        ),
        (
            "playrate",
            Project(
                **base,
                tracks=(
                    Track(
                        name="T",
                        items=(dataclasses.replace(good_item, playrate=2.0),),
                    ),
                ),
            ),
        ),
        (
            "folder",
            Project(**base, tracks=(Track(name="T", folder_kind=1, folder_depth=1),)),
        ),
        (
            "send",
            Project(
                **base,
                tracks=(Track(name="T", sends=(Send(source_track=0),)),),
            ),
        ),
        (
            "unnamed item",
            Project(
                **base,
                tracks=(
                    Track(name="T", items=(dataclasses.replace(good_item, name=None),)),
                ),
            ),
        ),
        (
            "quote in name",
            Project(**base, tracks=(Track(name='bad "name"'),)),
        ),
        (
            "region end",
            Project(**base, regions=(Region("r", 5.0, 5.0),)),
        ),
        (
            "unsorted midi",
            Project(
                **base,
                tracks=(
                    Track(
                        name="M",
                        items=(
                            Item(
                                position_s=0.0,
                                length_s=1.0,
                                source=MidiSource(
                                    events=(
                                        MidiEvent(480, 0x90, 60, 100),
                                        MidiEvent(0, 0x80, 60, 0),
                                    )
                                ),
                                name="m.mid",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ]
    return cases


@pytest.mark.parametrize(
    "label, project", _invalid_cases(), ids=[label for label, _ in _invalid_cases()]
)
def test_validator_refuses_and_sink_writes_nothing(tmp_path: Path, label, project):
    assert validate(project), "the validator accepted an unwritable project: %s" % label
    path = tmp_path / ("%s.rpp" % label.replace(" ", "_"))
    with pytest.raises(ValidationError):
        FileSink().write(project, path)
    assert not path.exists(), "a refused project created a file"
    assert list(tmp_path.iterdir()) == []
