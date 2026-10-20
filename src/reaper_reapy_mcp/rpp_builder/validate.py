"""Pure pre-write checks: if anything is wrong, not one byte is written.

Every refusal below is a measured limitation, not a preference:

* folders/sends/FX/envelopes have a known encoding but no writer until F5
  (`PLAN` section 4), so a non-default value must not be silently dropped;
* fades and playrate other than the measured constants have no reference line;
* every reference item carries `NAME`, and every reference `SM` record uses
  mode `+`;
* quotes in names/paths cannot be escaped (no escape measured).

The function returns a list of messages; `FileSink` raises before touching the
disk, and the test suite asserts that an invalid model leaves no file.
"""

from __future__ import annotations

import re

from .model.item import AudioSource, Item, MidiSource
from .model.project import Project
from .model.track import Track

_GUID = re.compile(r"^\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}$")

#: Fields whose grammar is measured but whose writer is F5.
_F5_FIELDS = ("folder_kind", "folder_depth", "folder_compact")


def _check_text(label: str, text: str, problems: list[str]) -> None:
    if '"' in text:
        problems.append(
            "%s contains a double quote, and REAPER has no measured escape: %r"
            % (label, text)
        )


def _check_guid(label: str, guid: str | None, problems: list[str]) -> None:
    if guid is not None and not _GUID.match(guid):
        problems.append(
            "%s must be {UPPER-CASE-UUID}; got %r" % (label, guid)
        )


def _check_track(index: int, track: Track, problems: list[str]) -> None:
    prefix = "track %d (%r)" % (index, track.name)
    _check_text(prefix + " name", track.name, problems)
    _check_guid(prefix + " guid", track.guid, problems)
    for field in _F5_FIELDS:
        if getattr(track, field):
            problems.append(
                "%s.%s is not writable until F5 (the encoding is measured, the "
                "writer row is not)" % (prefix, field)
            )
    if track.sends:
        problems.append(
            "%s has %d send(s); AUXRECV writing arrives in F5" % (prefix, len(track.sends))
        )
    if track.fx:
        problems.append(
            "%s has %d FX plugin(s); FXCHAIN writing arrives in F5" % (prefix, len(track.fx))
        )
    if track.envelopes:
        problems.append(
            "%s has %d envelope(s); no reference with a track envelope exists" % (prefix, len(track.envelopes))
        )
    if track.channels < 1:
        problems.append("%s.channels must be >= 1" % prefix)

    for item_index, item in enumerate(track.items):
        _check_item("%s item %d" % (prefix, item_index), item, problems)


def _check_item(prefix: str, item: Item, problems: list[str]) -> None:
    if item.length_s <= 0:
        problems.append("%s.length_s must be > 0" % prefix)
    if item.position_s < 0:
        problems.append("%s.position_s must be >= 0" % prefix)
    if item.source_offset_s < 0:
        problems.append("%s.source_offset_s must be >= 0" % prefix)
    if item.name is None:
        problems.append(
            "%s has no name; every reference item writes NAME, so an unnamed item "
            "is a grammar variant nobody has measured" % prefix
        )
    else:
        _check_text(prefix + " name", item.name, problems)
    if item.fade_in_s or item.fade_out_s:
        problems.append(
            "%s has a fade; only the zero-length FADEIN/FADEOUT line is measured" % prefix
        )
    if item.playrate != 1.0:
        problems.append(
            "%s.playrate is %r; only 1.0 is measured (PLAYRATE 1 1 0 -1 0 0.0025)"
            % (prefix, item.playrate)
        )
    _check_guid(prefix + " guid", item.guid, problems)
    _check_guid(prefix + " take_guid", item.take_guid, problems)

    source = item.source
    if isinstance(source, AudioSource):
        if not source.file_path:
            problems.append("%s has an empty audio file_path" % prefix)
        else:
            _check_text(prefix + " audio file_path", source.file_path, problems)
        if item.stretch_markers:
            positions = [marker.project_pos_s for marker in item.stretch_markers]
            if positions != sorted(positions):
                problems.append("%s SM positions are not in ascending order" % prefix)
            for marker in item.stretch_markers:
                if marker.mode != "+":
                    problems.append(
                        "%s SM mode %r is not measured; all 846 reference records use '+'"
                        % (prefix, marker.mode)
                    )
    elif isinstance(source, MidiSource):
        if item.stretch_markers:
            problems.append(
                "%s is MIDI and has stretch markers; SM exists on audio items only" % prefix
            )
        if source.ppq <= 0:
            problems.append("%s.ppq must be > 0" % prefix)
        previous = -1
        for event_index, event in enumerate(source.events):
            if event.tick < 0:
                problems.append("%s event %d has a negative tick" % (prefix, event_index))
            if event.tick < previous:
                problems.append(
                    "%s events are not in non-decreasing tick order at event %d"
                    % (prefix, event_index)
                )
            previous = event.tick
            if not 0x80 <= event.status <= 0xFF:
                problems.append(
                    "%s event %d status %#x is not a MIDI status byte"
                    % (prefix, event_index, event.status)
                )
            if not 0 <= event.data1 <= 0xFF or not 0 <= event.data2 <= 0xFF:
                problems.append(
                    "%s event %d data bytes must be 0..255" % (prefix, event_index)
                )
        for raw_line in source.extra_raw:
            if "\n" in raw_line or "\r" in raw_line:
                problems.append(
                    "%s extra_raw must hold single lines without terminators" % prefix
                )
    else:
        problems.append("%s has an unknown source type %r" % (prefix, type(source).__name__))


def validate(project: Project) -> list[str]:
    """Return every reason the project cannot be written faithfully."""
    problems: list[str] = []
    if project.tempo_bpm <= 0:
        problems.append("tempo_bpm must be > 0")
    numerator, denominator = project.meter
    if numerator <= 0 or denominator <= 0:
        problems.append("meter must be positive")
    if project.sample_rate <= 0:
        problems.append("sample_rate must be > 0")
    if not project.grid_line:
        problems.append("grid_line must hold the measured GRID tokens")
    _check_text("project title", project.title, problems)

    for index, track in enumerate(project.tracks):
        _check_track(index, track, problems)

    for index, marker in enumerate(project.markers):
        prefix = "marker %d (%r)" % (index, marker.name)
        _check_text(prefix + " name", marker.name, problems)
        _check_guid(prefix + " guid", marker.guid, problems)
        if marker.pos_s < 0:
            problems.append("%s.pos_s must be >= 0" % prefix)
    for index, region in enumerate(project.regions):
        prefix = "region %d (%r)" % (index, region.name)
        _check_text(prefix + " name", region.name, problems)
        _check_guid(prefix + " guid", region.guid, problems)
        if region.start_s < 0:
            problems.append("%s.start_s must be >= 0" % prefix)
        if region.end_s <= region.start_s:
            problems.append("%s must end after it starts" % prefix)
    return problems
