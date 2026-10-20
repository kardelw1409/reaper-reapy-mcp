"""Items and their sources.

Grammar: `Docs/RPP_FORMAT.md` sections 5 (item fields), 6 (`SM` triples) and 7
(`SOURCE WAVE` / `SOURCE MIDI`). One take per item is written inline: no
`<TAKE` block exists in any reference, so none is modelled here.

`MidiEvent.tick` is ABSOLUTE; the file stores DELTAS between events. The
conversion is the writer's job and the inverse is the reader's - mixing the two
is risk R2, and it is the kind of error that opens fine and sounds wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MidiEvent:
    """One MIDI event at an absolute tick inside its source."""

    tick: int
    status: int  # 0x90 note-on, 0x80 note-off, 0xb0 control change, ...
    data1: int
    data2: int


@dataclass(frozen=True, slots=True)
class StretchMarker:
    """One `SM` record: position on the ruler, position in the source, mode.

    Only mode `+` was measured (846 records, 0 exceptions), so the writer
    refuses any other mode rather than guessing its meaning.
    """

    project_pos_s: float
    source_pos_s: float
    mode: str = "+"


@dataclass(frozen=True, slots=True)
class AudioSource:
    """A `<SOURCE WAVE` block: one file, always quoted in the file."""

    file_path: str


@dataclass(frozen=True, slots=True)
class MidiSource:
    """A `<SOURCE MIDI` block with embedded events.

    `ppq` is per source, not global (480 and 960 are both measured). The fixed
    tail (`CCINTERP`, `CHASE_CC_TAKEOFFS`, `IGNTEMPO`, `EVTFILTER`) carries the
    measured values; only `source_color` varies in the reference.

    `extra_raw` carries a nested block this layer does not model (the optional
    `<X ...>` base64 blob, measured in 1 of 51 sources). The lines are stored
    verbatim, indentation included, and re-emitted after `POOLEDEVTS` - the
    position measured in the reference. It is a deliberate escape hatch, not a
    second grammar.
    """

    ppq: int = 480
    events: tuple[MidiEvent, ...] = ()
    pooled_events_guid: str | None = None
    guid: str | None = None  # the source's own GUID (the second one in the block)
    source_color: int = 564
    extra_raw: tuple[str, ...] = field(default=())


@dataclass(slots=True)
class Item:
    """One `<ITEM` block. `name` is required in practice: every reference item
    writes `NAME`, and a file without it would be a grammar variant nobody has
    measured.
    """

    position_s: float
    length_s: float
    source: AudioSource | MidiSource
    source_offset_s: float = 0.0  # SOFFS[0]: seconds into the source
    name: str | None = None
    loop: bool = True
    selected: bool = False
    playrate: float = 1.0  # only 1.0 measured; validate refuses anything else
    fade_in_s: float = 0.0  # only 0 measured; validate refuses anything else
    fade_out_s: float = 0.0
    guid: str | None = None  # IGUID (item id)
    take_guid: str | None = None  # GUID (take/source id)
    stretch_markers: tuple[StretchMarker, ...] = ()
    #: Unknown fields, in file order: `((token, (arg, ...)), ...)`.
    extra: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def is_midi(self) -> bool:
        return isinstance(self.source, MidiSource)
