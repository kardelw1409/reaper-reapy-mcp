"""Standard MIDI File reader: enough of SMF to build a REAPER MIDI source.

The bundle's files are format 0 or 1 with one track each (measured: all 9 files
of the aligned_v4 bundle are `MThd 0006 0000 0001 <ppq>`), so this reader handles
exactly that: one or more `MTrk` chunks concatenated in file order, running
status, note on/off and the channel messages REAPER can carry.

Three measured facts it exists for:

* `division` (PPQ) is PER FILE and must travel into `HASDATA 1 <ppq> QN`
  (RPP_FORMAT.md section 7.2);
* the take's NAME is `"<file track name> - <file name>"` when the file carries a
  track-name meta event, and just the file name otherwise - measured on the
  reference: `ins.mid` holds `Ins` and the take is `NAME "Ins - ins.mid"`, while
  `vocal.mid` holds none and the take is bare `NAME vocal.mid`;
* `end_tick` is the last tick the file mentions (including the end-of-track
  event), which is what REAPER turns into the item length rounded UP to whole
  beats (measured live: 295322 ticks -> 616 beats, 286534 -> 597, 2937xx -> 612).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


class MidiFileError(ValueError):
    """The file is not a standard MIDI file this reader can carry."""


@dataclass(frozen=True, slots=True)
class MidiFileEvent:
    """One channel event of the file, at its absolute tick."""

    tick: int
    status: int
    data1: int
    data2: int

    @property
    def kind(self) -> int:
        return self.status & 0xF0

    @property
    def is_note_on(self) -> bool:
        return self.kind == 0x90 and self.data2 > 0

    @property
    def is_note_off(self) -> bool:
        return self.kind == 0x80 or (self.kind == 0x90 and self.data2 == 0)


@dataclass(frozen=True, slots=True)
class MidiFile:
    """A parsed file: PPQ, track name (if any), channel events, last tick."""

    ppq: int
    track_name: str | None
    events: tuple[MidiFileEvent, ...]
    end_tick: int


def _read_varlen(data: bytes, index: int) -> tuple[int, int]:
    value = 0
    while True:
        if index >= len(data):
            raise MidiFileError("truncated variable-length quantity")
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, index


def _decode_text(raw: bytes) -> str | None:
    for encoding in ("utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        text = text.strip("\x00").strip()
        return text or None
    return None


def read_midi_file(path: str | Path) -> MidiFile:
    """Parse a standard MIDI file into PPQ, track name, channel events, end tick."""
    data = Path(path).read_bytes()
    if data[:4] != b"MThd":
        raise MidiFileError("%s does not start with MThd" % path)
    header_length = struct.unpack(">I", data[4:8])[0]
    if header_length < 6:
        raise MidiFileError("%s has a short header (%d bytes)" % (path, header_length))
    _format, track_count, ppq = struct.unpack(">HHH", data[8:14])
    if ppq & 0x8000:
        raise MidiFileError(
            "%s uses SMPTE timing (%#x), which REAPER expresses differently" % (path, ppq)
        )
    if ppq <= 0:
        raise MidiFileError("%s declares PPQ 0" % path)

    events: list[MidiFileEvent] = []
    track_name: str | None = None
    end_tick = 0
    index = 8 + header_length
    tracks_read = 0
    while index + 8 <= len(data) and tracks_read < max(track_count, 1):
        chunk_id = data[index : index + 4]
        length = struct.unpack(">I", data[index + 4 : index + 8])[0]
        chunk = data[index + 8 : index + 8 + length]
        index += 8 + length
        if chunk_id != b"MTrk":
            continue
        tracks_read += 1
        position = 0
        offset = 0
        running = 0
        while offset < len(chunk):
            delta, offset = _read_varlen(chunk, offset)
            position += delta
            end_tick = max(end_tick, position)
            if offset >= len(chunk):
                break
            status = chunk[offset]
            if status < 0x80:
                status = running  # running status: the byte is already data
            else:
                offset += 1
                if status < 0xF0:
                    running = status
            kind = status & 0xF0
            if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                if offset + 2 > len(chunk):
                    break
                events.append(MidiFileEvent(position, status, chunk[offset], chunk[offset + 1]))
                offset += 2
            elif kind in (0xC0, 0xD0):
                if offset + 1 > len(chunk):
                    break
                events.append(MidiFileEvent(position, status, chunk[offset], 0))
                offset += 1
            elif status == 0xFF:
                if offset >= len(chunk):
                    break
                meta = chunk[offset]
                offset += 1
                size, offset = _read_varlen(chunk, offset)
                payload = chunk[offset : offset + size]
                offset += size
                if meta == 0x03 and track_name is None:
                    track_name = _decode_text(payload)
                elif meta == 0x2F:
                    end_tick = max(end_tick, position)
                    break
            elif status in (0xF0, 0xF7):
                size, offset = _read_varlen(chunk, offset)
                offset += size
            else:
                raise MidiFileError(
                    "%s: unhandled status %#x at tick %d" % (path, status, position)
                )
    return MidiFile(
        ppq=int(ppq),
        track_name=track_name,
        events=tuple(events),
        end_tick=end_tick,
    )


def take_name(midi: MidiFile, file_name: str) -> str:
    """The take name REAPER gives an inserted MIDI file (measured, see module doc)."""
    if midi.track_name:
        return "%s - %s" % (midi.track_name, file_name)
    return file_name
