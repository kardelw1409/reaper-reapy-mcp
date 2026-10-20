"""Node tree -> `Project` (the inverse of `writer.to_nodes`).

This reader covers exactly what the F2 writer emits: flat tracks, audio/MIDI
items, `SM`, markers/regions, tempo, meter and the measured settings. Unknown
FIELDS are carried into each entity's `extra`; unknown nested blocks are carried
only inside a MIDI source (`extra_raw`), which is where the one measured `<X>`
blob lives. An unknown block elsewhere is ignored - the reader does not claim to
preserve a foreign project byte for byte (that is the F3+ question, and the
node layer already can).

`Project.title` is not a file field (see `model/project.py`); the caller passes
it, usually the file stem.
"""

from __future__ import annotations

from ..model.item import (
    AudioSource,
    Item,
    MidiEvent,
    MidiSource,
    StretchMarker,
)
from ..model.project import Marker, Project, Region
from ..model.track import Track
from ..rpp.node import RppDocument, RppField, RppNode, unquote
from ..rpp.render import render
from ..writer.grammar import PROJECT_KNOWN_TOKENS
_TRACK_FIELDS = {
    "NAME",
    "PEAKCOL",
    "BEAT",
    "AUTOMODE",
    "PANLAWFLAGS",
    "VOLPAN",
    "MUTESOLO",
    "IPHASE",
    "PLAYOFFS",
    "ISBUS",
    "BUSCOMP",
    "SHOWINMIX",
    "FIXEDLANES",
    "SEL",
    "REC",
    "VU",
    "TRACKHEIGHT",
    "INQ",
    "NCHAN",
    "FX",
    "TRACKID",
    "PERF",
    "MIDIOUT",
    "MAINSEND",
}
_ITEM_FIELDS = {
    "POSITION",
    "SNAPOFFS",
    "LENGTH",
    "LOOP",
    "ALLTAKES",
    "FADEIN",
    "FADEOUT",
    "MUTE",
    "SEL",
    "IGUID",
    "IID",
    "NAME",
    "VOLPAN",
    "SOFFS",
    "PLAYRATE",
    "CHANMODE",
    "GUID",
    "SM",
}
_MIDI_FIELDS = {
    "HASDATA",
    "CCINTERP",
    "POOLEDEVTS",
    "CHASE_CC_TAKEOFFS",
    "GUID",
    "IGNTEMPO",
    "SRCCOLOR",
    "EVTFILTER",
}


def _float(token: str) -> float:
    return float(token)


def _int(token: str) -> int:
    return int(token)


def _extra(fields: list[RppField], known: set[str], skip: set[str] = frozenset()) -> tuple:
    return tuple(
        (field.tokens[0], field.tokens[1:])
        for field in fields
        if field.tokens and field.tokens[0] not in known and field.tokens[0] not in skip
    )


def _project_fields(root: RppNode) -> dict[str, RppField]:
    result: dict[str, RppField] = {}
    for field in root.fields():
        if field.key and field.key not in result:
            result[field.key] = field
    return result


def _read_markers_and_regions(root: RppNode) -> tuple[list[Marker], list[Region]]:
    """Pair marker lines the way the reference is written (RPP_FORMAT section 3)."""
    markers: list[Marker] = []
    regions: list[Region] = []
    opening: dict[str, tuple[str, float, str]] = {}
    order: list[str] = []

    for field in root.fields():
        if field.key != "MARKER":
            continue
        args = field.args
        if len(args) == 4 and args[2] == '""':
            marker_id, position = args[0], float(args[1])
            if marker_id not in opening:
                raise ValueError("MARKER %s closes a region that was never opened" % marker_id)
            name, start, guid = opening.pop(marker_id)
            regions.append(Region(name=name, start_s=start, end_s=position, guid=guid))
            continue
        if len(args) >= 9 and args[6] == "R":
            marker_id = args[0]
            position = float(args[1])
            name = unquote(args[2])
            guid = args[7]
            if args[3] == "1":  # region opening (measured token 5 = 1)
                opening[marker_id] = (name, position, guid)
                order.append(marker_id)
            else:  # plain marker (measured token 5 = 0)
                markers.append(Marker(name=name, pos_s=position, guid=guid))
            continue
        raise ValueError("unrecognised MARKER form: %r" % (" ".join(field.tokens),))

    if opening:
        raise ValueError("region(s) never closed: %r" % sorted(opening))
    return markers, regions


def _read_stretch_markers(item: RppNode) -> tuple[StretchMarker, ...]:
    """Read `SM` records, including REAPER's line wrapping.

    MEASURED on the reference (RPP_FORMAT.md section 6): REAPER wraps a long
    `SM` run over several lines and OMITS the mode token on the LAST record of
    each line - 145 records on 4 lines hold 141 `+` tokens, and the token counts
    are 125/125/116/65, i.e. `3N - 1` per line. A line whose token count is
    divisible by 3 (the shape this package's own writer emits, one record per
    line) carries a mode on every record. Both shapes are accepted; the missing
    mode means the default, `+`.
    """
    records: list[StretchMarker] = []
    for field in item.fields():
        if field.key != "SM":
            continue
        args = field.args
        trailing = len(args) % 3 == 0
        if not trailing and (len(args) + 1) % 3 == 0:
            args = args + ("+",)  # the last record of the line has no mode
        elif not trailing:
            raise ValueError("SM line is not a sequence of triples: %r" % (field.args,))
        for index in range(0, len(args), 3):
            records.append(
                StretchMarker(
                    project_pos_s=float(args[index]),
                    source_pos_s=float(args[index + 1]),
                    mode=args[index + 2],
                )
            )
    return tuple(records)


def _read_midi_source(node: RppNode) -> MidiSource:
    ppq = 480
    pooled: str | None = None
    source_guid: str | None = None
    color = 564
    events: list[MidiEvent] = []
    extra_raw: list[str] = []
    tick = 0
    for child in node.body:
        if isinstance(child, RppNode):
            text = render(child)
            extra_raw.extend(text.splitlines())
            continue
        if not child.tokens:
            continue
        key = child.key
        args = child.args
        if key == "HASDATA":
            ppq = _int(args[1])
        elif key == "POOLEDEVTS":
            pooled = args[0]
        elif key == "GUID":
            source_guid = args[0]
        elif key == "SRCCOLOR":
            color = _int(args[0])
        elif key == "E":
            tick += _int(args[0])
            events.append(
                MidiEvent(
                    tick=tick,
                    status=int(args[1], 16),
                    data1=int(args[2], 16),
                    data2=int(args[3], 16),
                )
            )
    # The writer terminates every source with CC123 (measured in 51/51 reference
    # sources). It is not part of the musical content, so it is not a model event:
    # dropping it keeps write -> read -> write stable.
    if (
        events
        and events[-1].status == 0xB0
        and events[-1].data1 == 0x7B
        and events[-1].data2 == 0
    ):
        events.pop()
    return MidiSource(
        ppq=ppq,
        events=tuple(events),
        pooled_events_guid=pooled,
        guid=source_guid,
        source_color=color,
        extra_raw=tuple(extra_raw),
    )


def _read_source(item: RppNode) -> AudioSource | MidiSource:
    node = item.find("SOURCE")
    if node is None:
        raise ValueError("ITEM has no SOURCE block")
    kind = node.args[0] if node.args else ""
    if kind == "WAVE":
        file_field = node.field("FILE")
        if file_field is None:
            raise ValueError("SOURCE WAVE has no FILE line")
        return AudioSource(file_path=unquote(file_field.value))
    if kind == "MIDI":
        return _read_midi_source(node)
    raise ValueError("unknown SOURCE kind %r" % (kind,))


def _read_item(node: RppNode) -> Item:
    values = {field.key: field for field in node.fields() if field.key}
    try:
        position = _float(values["POSITION"].args[0])
        length = _float(values["LENGTH"].args[0])
    except KeyError as exc:
        raise ValueError("ITEM has no %s line" % exc) from None
    source = _read_source(node)
    soffs = values.get("SOFFS")
    source_offset = _float(soffs.args[0]) if soffs is not None and soffs.args else 0.0
    name_field = values.get("NAME")
    name = unquote(name_field.value) if name_field is not None else None
    return Item(
        position_s=position,
        length_s=length,
        source=source,
        source_offset_s=source_offset,
        name=name if name != "" else None,
        loop=(values["LOOP"].args[0] != "0") if "LOOP" in values else True,
        selected=(values["SEL"].args[0] != "0") if "SEL" in values else False,
        iid=_int(values["IID"].args[0]) if "IID" in values and values["IID"].args else None,
        playrate=_float(values["PLAYRATE"].args[0]) if "PLAYRATE" in values else 1.0,
        guid=values["IGUID"].args[0] if "IGUID" in values and values["IGUID"].args else None,
        take_guid=values["GUID"].args[0] if "GUID" in values and values["GUID"].args else None,
        stretch_markers=_read_stretch_markers(node),
        extra=_extra(list(node.fields()), _ITEM_FIELDS),
    )


def _read_track(node: RppNode) -> Track:
    values = {field.key: field for field in node.fields() if field.key}
    name_field = values.get("NAME")
    name = unquote(name_field.value) if name_field is not None else ""
    volpan = values.get("VOLPAN")
    volume = _float(volpan.args[0]) if volpan is not None and volpan.args else 1.0
    pan = _float(volpan.args[1]) if volpan is not None and len(volpan.args) > 1 else 0.0
    mutesolo = values.get("MUTESOLO")
    mute = bool(mutesolo is not None and mutesolo.args and mutesolo.args[0] != "0")
    solo = bool(mutesolo is not None and len(mutesolo.args) > 1 and mutesolo.args[1] != "0")
    isbus = values.get("ISBUS")
    folder_kind = _int(isbus.args[0]) if isbus is not None and isbus.args else 0
    folder_depth = _int(isbus.args[1]) if isbus is not None and len(isbus.args) > 1 else 0
    buscomp = values.get("BUSCOMP")
    compact = _int(buscomp.args[0]) if buscomp is not None and buscomp.args else 0
    peakcol = values.get("PEAKCOL")
    return Track(
        name=name,
        guid=node.args[0] if node.args else None,
        color=_int(peakcol.args[0]) if peakcol is not None and peakcol.args else 16576,
        volume=volume,
        pan=pan,
        mute=mute,
        solo=solo,
        channels=_int(values["NCHAN"].args[0]) if "NCHAN" in values else 2,
        folder_kind=folder_kind,
        folder_depth=folder_depth,
        folder_compact=compact,
        items=tuple(_read_item(child) for child in node.find_all("ITEM")),
        extra=_extra(list(node.fields()), _TRACK_FIELDS),
    )


def from_nodes(document: RppDocument, title: str = "") -> Project:
    """Read a document produced by (or compatible with) `writer.to_nodes`."""
    root = document.root
    if root is None or root.tag != "REAPER_PROJECT":
        raise ValueError("document has no <REAPER_PROJECT> root block")
    fields = _project_fields(root)

    tempo = _float(fields["TEMPO"].args[0]) if "TEMPO" in fields else 120.0
    meter = (4, 4)
    if "TEMPO" in fields and len(fields["TEMPO"].args) >= 3:
        meter = (_int(fields["TEMPO"].args[1]), _int(fields["TEMPO"].args[2]))
    sample_rate = _int(fields["SAMPLERATE"].args[0]) if "SAMPLERATE" in fields else 44100
    grid = tuple(fields["GRID"].args) if "GRID" in fields else ()
    master = fields.get("MASTER_VOLUME")
    master_volume = _float(master.args[0]) if master is not None and master.args else 1.0
    master_pan = _float(master.args[1]) if master is not None and len(master.args) > 1 else 0.0

    notes_node = root.find("NOTES")
    notes = ""
    if notes_node is not None:
        notes = "\n".join(
            " ".join(field.tokens) if field.tokens else ""
            for field in notes_node.fields()
        )

    markers, regions = _read_markers_and_regions(root)
    return Project(
        title=title,
        tempo_bpm=tempo,
        meter=meter,
        sample_rate=sample_rate,
        grid_line=grid,
        master_volume=master_volume,
        master_pan=master_pan,
        tracks=tuple(_read_track(node) for node in root.find_all("TRACK")),
        markers=tuple(markers),
        regions=tuple(regions),
        notes=notes,
        extra=_extra(list(root.fields()), PROJECT_KNOWN_TOKENS | {"MARKER"}),
    )
