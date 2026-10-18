"""The legend for reading a REAPER arrangement: `HOW_TO_READ`.

Returned by `vision_state` as the `how_to_read` field and mirrored by the CLI. It
lives in its own module with no dependencies so that the CLI (a plain script that
must run without the MCP package) and the tool layer can share exactly the same
text - two copies would drift, and a legend that disagrees with the tools is worse
than none.

Facts measured on this host (REAPER 7.53, 2026-10-18) are marked "[measured]";
everything else is standard REAPER behaviour and holds on another machine. The
split is deliberate: a caller can tell which statements are about THIS project and
theme, and which travel.
"""

from __future__ import annotations

__all__ = ["HOW_TO_READ"]

HOW_TO_READ: dict[str, list[str]] = {
    "units": [
        "Every time in every answer is SECONDS of project time. The visible range is"
        " `view` = [start_s, end_s].",
        "Bars on the ruler map to seconds as: bar = time_s / bar_seconds + 1, beat ="
        " (time_s % bar_seconds) / beat_seconds + 1. Both are given in `project`"
        " (read from the metre - a bar is numerator * (4/denominator) * 60/tempo,"
        " so a 2/4 metre makes a bar HALF of what a 4/4 assumption gives: this"
        " project is 2/4 at 150.121 BPM, so 16 s is 20 bars, not 10).",
        "Pixels map to time with `h_zoom_px_per_s`: time = (x - x0) / px_per_s, where"
        " x0 is the arrangement's left edge.",
    ],
    "window": [
        "The ruler is a strip at the top of the arrangement and shows BOTH bar.beat"
        " (upper row) and min:sec (lower row) [measured].",
        "Below the ruler is the region/marker lane: regions are coloured blocks with"
        " names, spanning the seconds they cover; markers are small flags with"
        " names. Region names are how a song's structure is read at a glance.",
        "The track panel (TCP) on the left carries, per track: number, name, the"
        " M/S/FX/IN controls and a colour strip. The arrangement (items) is to its"
        " right.",
        "One track = one row in the TCP = one horizontal band in the arrangement.",
        "The transport at the bottom shows the position as bar.beat.tick AND"
        " min:sec.msec, the tempo, the metre, the state ([Stopped]/[Playing]) and"
        " an audio-device line [measured].",
    ],
    "items": [
        "An audio item is a filled rectangle: a label strip along the top carrying"
        " the file name (e.g. song.wav) and, when REAPER cannot read the source, the"
        " word OFFLINE; below it the waveform is drawn once the peaks exist"
        " [measured].",
        "The drawn waveform is DARK ink on a lighter fill in this theme (fill grey"
        " 100-150, ink 20-38) - not a bright line on a dark fill [measured].",
        "A MIDI item shows note blocks. Dense passages are obvious, but LONG"
        " sustained notes can be a single faint horizontal line: measured, 59 bass"
        " notes read as \"empty\" on an 8 s view.",
        "Item edges are the true start and end times of the item.",
    ],
    "grid": [
        "Vertical grid lines mark bars; when zoomed in far enough they also mark"
        " beats. Counting them gives the real bar spacing in pixels, which is how a"
        " pixel distance becomes a time [measured: grid lines are the darkest"
        " lines].",
        "A horizontal line at the bottom of the arrangement is the scrollbar, not"
        " part of the arrangement: x0 + span * px_per_s must leave room for it.",
    ],
    "cursors": [
        "The EDIT cursor is a vertical line through the whole arrangement; in this"
        " theme it is cyan/teal, mean grey ~133 - NOT white [measured]. Do not look"
        " for a white column.",
        "A play cursor is drawn only while the transport runs.",
    ],
    "workflow": [
        "1. `vision_state` - seconds range, tempo/metre/bar_seconds/beat_seconds,"
        " track count, project length, and the media state. No image: cheap.",
        "2. `vision_scan` - ONE contact sheet over a time range, every tile"
        " captioned with its seconds and bars; or `vision_view` + `vision_shot` for"
        " a single range.",
        "3. `vision_shot` with `crop` to read small text: track names, region and"
        " marker labels, the transport.",
        "4. For exact events - MIDI notes, item positions, source files - use the"
        " REAPER data tools. The picture is for LAYOUT; the numbers come from the"
        " API.",
    ],
    "trust": [
        "A frame that looks empty is NOT proof that an item is empty: it is proof"
        " that nothing was drawn there (measured on long MIDI notes and on unloaded"
        " media).",
        "If items show no waveform, check `media` / `media_settings` in this answer"
        " first: REAPER's setting \"Set media items offline when application is not"
        " active\" makes it close a project's audio whenever its window is not the"
        " active application, and closed sources are drawn without waveforms even"
        " though the files exist.",
        "A frame can show LESS than `view` reports. The range is what REAPER holds"
        " for the arrange; how much of it reaches the screen is decided by the"
        " window width MINUS the track panel, which is user-resizable and can be"
        " very wide (measured: a 4 s range needing 1572 px on a 1721 px window with"
        " a ~1000 px panel - the frame showed under half of the range). So VERIFY"
        " the scale from the ruler labels DRAWN IN THE FRAME, and if they do not"
        " span the reported range, the frame shows only part of it.",
        "Do not measure pixels on a downscaled file. The PNG saved next to an answer"
        " is exactly the image that was sent, so a `max_width` limit shrinks the"
        " FILE too: pass `max_width=None` for a frame you intend to measure, or"
        " correct every pixel distance by frame_width / image_width.",
        "Settle (two identical consecutive frames) proves the window is not MOVING,"
        " not that everything was repainted: a partially stale frame has been"
        " observed. Treat the ruler and the labels drawn in the same frame as the"
        " only trusted anchors, and keep measurements inside the area that changed.",
        "This server never activates, moves or clicks anything by default: captures"
        " are REAPER's own rendering and every answer carries the write witness"
        " (`state_change_delta`, 0 = the project was not touched).",
    ],
}
