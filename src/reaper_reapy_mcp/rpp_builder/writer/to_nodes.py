"""`Project` -> node tree, in the measured token order.

The node layer keeps strings verbatim; this module decides WHICH strings. Every
line's position is taken from `writer/grammar.py`, whose comments name the
reference line each constant came from.

Deterministic by construction (decision 4): every generated GUID is
`make_guid(...)` over stable parts, item ids run in project order, and no clock,
random source or host call is involved. Two builds of one model are byte-identical.
"""

from __future__ import annotations

from ..model.item import AudioSource, Item, MidiSource
from ..model.project import Project
from ..model.track import Track
from ..model.units import (
    format_exact,
    format_hex,
    format_number,
    format_seconds,
    make_guid,
    quote_always,
    quote_text,
)
from ..rpp.node import CRLF, RppDocument, RppField, RppNode
from . import grammar

#: Depth of each block, in 2-space levels (RPP_FORMAT.md section 1):
#: root 0, track 1, item 2, source 3, source field 4.
_ROOT = 0
_TRACK = 1
_ITEM = 2
_SOURCE = 3
_SOURCE_FIELD = 4


def _field(level: int, tokens: tuple[str, ...]) -> RppField:
    return RppField(tokens=tokens, indent=grammar.INDENT * level, eol=CRLF)


def _raw_line(text: str) -> RppField:
    """A line kept verbatim (unknown nested block): indentation is already in it."""
    return RppField(tokens=(), indent="", eol=CRLF, raw=text)


def _node(level: int, tag: str, args: tuple[str, ...], body: list) -> RppNode:
    indent = grammar.INDENT * level
    return RppNode(
        tag=tag,
        args=args,
        body=body,
        indent=indent,
        eol=CRLF,
        close_indent=indent,
        close_eol=CRLF,
    )


def _header_items(items: tuple, level: int) -> list[RppField | RppNode]:
    """Expand the declarative grammar tuples into node/field objects."""
    result: list[RppField | RppNode] = []
    for item in items:
        if item[0] == "field":
            _, token, args = item
            result.append(_field(level, (token,) + tuple(args)))
        else:
            _, tag, args, children = item
            result.append(
                _node(level, tag, tuple(args), _header_items(children, level + 1))
            )
    return result


def _master_envelopes(title: str, bpm: float) -> list[RppNode]:
    """`<MASTERPLAYSPEEDENV` + `<TEMPOENVEX` (reference lines 85-101).

    The tempo envelope's `PT` line is measured with fixed precision: 12 decimals
    for the position and 10 for the tempo (line 100). `str()` would not produce
    that, and a different text is a different file.
    """
    speed = _node(
        _TRACK,
        "MASTERPLAYSPEEDENV",
        (),
        [
            _field(2, ("EGUID", make_guid("master-speed-envelope", title))),
            _field(2, ("ACT", "0", "-1")),
            _field(2, ("VIS", "0", "1", "1")),
            _field(2, ("LANEHEIGHT", "0", "0")),
            _field(2, ("ARM", "0")),
            _field(2, ("DEFSHAPE", "0", "-1", "-1")),
        ],
    )
    tempo = _node(
        _TRACK,
        "TEMPOENVEX",
        (),
        [
            _field(2, ("EGUID", make_guid("tempo-envelope", title))),
            _field(2, ("ACT", "1", "-1")),
            _field(2, ("VIS", "1", "0", "1")),
            _field(2, ("LANEHEIGHT", "0", "0")),
            _field(2, ("ARM", "0")),
            _field(2, ("DEFSHAPE", "1", "-1", "-1")),
            _field(
                2,
                (
                    "PT",
                    "%.12f" % 0.0,
                    "%.10f" % float(bpm),
                    "1",
                    "262148",
                    "0",
                    "1",
                    "0",
                    '""',
                    "0",
                    "169",
                    "0",
                    "ABBB",
                ),
            ),
        ],
    )
    return [speed, tempo]


def _notes_block(project: Project) -> RppNode:
    body = [_field(2, (line,)) for line in project.notes.splitlines()]
    return _node(_TRACK, "NOTES", ("0", "2"), body)


def _marker_lines(project: Project) -> list[RppField]:
    """Markers first, then regions, each in model order (RPP_FORMAT section 3).

    Marker ids and region ids are written in ONE namespace, markers first, then
    regions. The reference reuses id 1 for its title marker and its first region,
    and MEASURED LIVE REAPER accepts that too (the region still read back as
    0.0..2.0), so this is not a correctness fix - it keeps ids unique so clients
    that index markers by id (reapy's Region API does) cannot pick the wrong row.
    """
    lines: list[RppField] = []
    for index, marker in enumerate(project.markers, start=1):
        guid = marker.guid or make_guid("marker", project.title, index, marker.name)
        lines.append(
            _field(
                _TRACK,
                (
                    "MARKER",
                    str(index),
                    format_seconds(marker.pos_s),
                    quote_text(marker.name),
                    "0",
                    "0",
                    "1",
                    "R",
                    guid,
                    "0",
                ),
            )
        )
    region_base = len(project.markers)
    for offset, region in enumerate(project.regions, start=1):
        index = region_base + offset
        guid = region.guid or make_guid("region", project.title, offset, region.name)
        lines.append(
            _field(
                _TRACK,
                (
                    "MARKER",
                    str(index),
                    format_seconds(region.start_s),
                    quote_text(region.name),
                    "1",
                    "0",
                    "1",
                    "R",
                    guid,
                    "0",
                ),
            )
        )
        lines.append(
            _field(
                _TRACK,
                ("MARKER", str(index), format_seconds(region.end_s), '""', "1"),
            )
        )
    return lines


def _track_fields(track: Track, guid: str) -> list[RppField]:
    values = {
        "NAME": (quote_text(track.name),),
        "PEAKCOL": (str(track.color),),
        "VOLPAN": (
            format_number(track.volume),
            format_number(track.pan),
            "-1",
            "-1",
            "1",
        ),
        "MUTESOLO": ("1" if track.mute else "0", "1" if track.solo else "0", "0"),
        "ISBUS": (str(track.folder_kind), str(track.folder_depth)),
        "BUSCOMP": (str(track.folder_compact), "0", "0", "0", "0"),
        "NCHAN": (str(track.channels),),
        "TRACKID": (guid,),
    }
    fields: list[RppField] = []
    for token, constant in grammar.TRACK_FIELD_ORDER:
        args = values[token] if constant is None else constant
        fields.append(_field(2, (token,) + tuple(args)))
    for token, args in track.extra:
        fields.append(_field(2, (token,) + tuple(args)))
    return fields


def _midi_source(
    project: Project,
    item: Item,
    source: MidiSource,
    track_index: int,
    item_index: int,
) -> RppNode:
    source_guid = source.guid or make_guid(
        "midi-source", project.title, track_index, item_index
    )
    pooled_guid = source.pooled_events_guid or make_guid(
        "pooled-events", project.title, track_index, item_index
    )
    body: list[RppField | RppNode] = [
        _field(_SOURCE_FIELD, ("HASDATA", "1", str(source.ppq), "QN")),
        _field(_SOURCE_FIELD, ("CCINTERP", "32")),
        _field(_SOURCE_FIELD, ("POOLEDEVTS", pooled_guid)),
    ]
    # Unknown nested blocks (the measured `<X ...>` blob): emitted verbatim at
    # the position measured in the reference, right after POOLEDEVTS.
    for raw_line in source.extra_raw:
        body.append(_raw_line(raw_line))
    previous = 0
    for event in source.events:
        delta = event.tick - previous
        previous = event.tick
        body.append(
            _field(
                _SOURCE_FIELD,
                (
                    "E",
                    str(delta),
                    format_hex(event.status),
                    format_hex(event.data1),
                    format_hex(event.data2),
                ),
            )
        )
    # MEASURED: all 51 reference MIDI sources end with CC123 (all notes off).
    # It is written ONE TICK AFTER the last event, not on the same tick: measured
    # live, a note-off on the LAST tick of the source is read back by REAPER with
    # endppq 0 (the note collapses), while the reference never has that shape.
    # The reference's gap varies (26..293760 ticks), so the presence is the
    # measured fact; +1 tick is the smallest safe position.
    if source.events:
        last_tick = source.events[-1].tick
        body.append(
            _field(
                _SOURCE_FIELD,
                ("E", "1", format_hex(0xB0), format_hex(0x7B), format_hex(0)),
            )
        )
    # Measured tail, with GUID and SRCCOLOR in their measured positions.
    body.append(_field(_SOURCE_FIELD, ("CCINTERP", "32")))
    body.append(_field(_SOURCE_FIELD, ("CHASE_CC_TAKEOFFS", "1")))
    body.append(_field(_SOURCE_FIELD, ("GUID", source_guid)))
    body.append(_field(_SOURCE_FIELD, ("IGNTEMPO", "0", "120", "4", "4")))
    body.append(_field(_SOURCE_FIELD, ("SRCCOLOR", str(source.source_color))))
    evtfilter = grammar.MIDI_SOURCE_TAIL[-1]
    body.append(_field(_SOURCE_FIELD, (evtfilter[0],) + evtfilter[1]))
    return _node(_SOURCE, "SOURCE", ("MIDI",), body)


def _source_node(
    project: Project, item: Item, track_index: int, item_index: int
) -> RppNode:
    source = item.source
    if isinstance(source, AudioSource):
        body = [_field(_SOURCE_FIELD, ("FILE", quote_always(source.file_path)))]
        return _node(_SOURCE, "SOURCE", ("WAVE",), body)
    assert isinstance(source, MidiSource)
    return _midi_source(project, item, source, track_index, item_index)


def _item_node(
    project: Project, item: Item, track_index: int, item_index: int, iid: int
) -> RppNode:
    """One `<ITEM`: fields in measured order, then optional SM, then the source."""
    guid = item.guid or make_guid(
        "item",
        project.title,
        track_index,
        item_index,
        format_seconds(item.position_s),
    )
    take_guid = item.take_guid or make_guid(
        "take", project.title, track_index, item_index
    )
    if isinstance(item.source, MidiSource):
        # SOFFS2 is the same offset in quarter notes: SOFFS1 * (bpm / 60).
        # This exact operation order reproduces REAPER's stored double (measured:
        # 23.35602 * (174.6873 / 60) == 68.00000120909999, while
        # 23.35602 * 174.6873 / 60 == 68.0000012091 - a different last digit).
        soffs = (
            format_seconds(item.source_offset_s),
            format_exact(item.source_offset_s * (project.tempo_bpm / 60.0)),
        )
    else:
        soffs = (format_seconds(item.source_offset_s),)
    values = {
        "POSITION": (format_seconds(item.position_s),),
        "LENGTH": (format_seconds(item.length_s),),
        "LOOP": ("1" if item.loop else "0",),
        "SEL": ("1" if item.selected else "0",),
        "IGUID": (guid,),
        "IID": (str(iid),),
        "NAME": (quote_text(item.name or ""),),
        "SOFFS": soffs,
        "GUID": (take_guid,),
    }
    body: list[RppField | RppNode] = []
    for token, constant in grammar.ITEM_FIELD_ORDER:
        args = values[token] if constant is None else constant
        body.append(_field(3, (token,) + tuple(args)))
    for token, args in item.extra:
        body.append(_field(3, (token,) + tuple(args)))
    for marker in item.stretch_markers:
        body.append(
            _field(
                3,
                (
                    "SM",
                    format_seconds(marker.project_pos_s),
                    format_seconds(marker.source_pos_s),
                    marker.mode,
                ),
            )
        )
    body.append(_source_node(project, item, track_index, item_index))
    return _node(_ITEM, "ITEM", (), body)


def _track_node(
    project: Project, track: Track, track_index: int, first_iid: int
) -> tuple[RppNode, int]:
    guid = track.guid or make_guid("track", project.title, track_index, track.name)
    body: list[RppField | RppNode] = _track_fields(track, guid)
    iid = first_iid
    for item_index, item in enumerate(track.items):
        body.append(_item_node(project, item, track_index, item_index, iid))
        iid += 1
    return _node(_TRACK, "TRACK", (guid,), body), iid


def to_nodes(project: Project) -> RppDocument:
    """Build the complete document tree for a project."""
    root_body: list[RppField | RppNode] = [_notes_block(project)]

    # Settings with the four manageable fields substituted; everything else is
    # a measured constant (`writer/grammar.py`).
    for item in grammar.PROJECT_HEAD_ITEMS:
        if item[0] == "field":
            _, token, args = item
            if token == "GRID":
                args = tuple(project.grid_line)
            elif token == "SAMPLERATE":
                args = (str(project.sample_rate), "0", "0")
            root_body.append(_field(_TRACK, (token,) + tuple(args)))
        else:
            _, tag, args, children = item
            root_body.append(
                _node(_TRACK, tag, tuple(args), _header_items(children, 2))
            )

    root_body.append(
        _field(
            _TRACK,
            (
                "TEMPO",
                format_number(project.tempo_bpm),
                str(project.meter[0]),
                str(project.meter[1]),
                "0",
            ),
        )
    )
    root_body.append(_field(_TRACK, ("PLAYRATE", "1", "0", "0.25", "4")))
    for _, token, args in grammar.PROJECT_MASTER_ITEMS:
        if token == "MASTER_VOLUME":
            args = (
                format_number(project.master_volume),
                format_number(project.master_pan),
                "-1",
                "-1",
                "1",
            )
        else:
            args = tuple(args)
        root_body.append(_field(_TRACK, (token,) + args))
    root_body.extend(_master_envelopes(project.title, project.tempo_bpm))
    root_body.extend(_marker_lines(project))
    root_body.append(_node(_TRACK, "PROJBAY", (), []))
    for token, args in project.extra:
        root_body.append(_field(_TRACK, (token,) + tuple(args)))

    iid = 1
    for track_index, track in enumerate(project.tracks):
        node, iid = _track_node(project, track, track_index, iid)
        root_body.append(node)

    root = _node(_ROOT, grammar.ROOT_TAG, grammar.ROOT_ARGS, root_body)
    return RppDocument(body=[root])
