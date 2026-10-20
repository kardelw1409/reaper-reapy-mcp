"""The domain model: a REAPER project in domain terms, no text knowledge.

Nothing in this package knows that `.rpp` is a text format (ARCHITECTURE
section 4). Values are numbers and identifiers; the writer turns them into
tokens and the reader turns tokens back.
"""

from __future__ import annotations

from .envelope import Envelope, EnvelopePoint
from .item import (
    AudioSource,
    Item,
    MidiEvent,
    MidiSource,
    StretchMarker,
)
from .project import DEFAULT_GRID_LINE, Marker, Project, Region
from .track import FxPlugin, Send, Track
from .units import (
    GUID_NAMESPACE,
    format_exact,
    format_hex,
    format_number,
    format_seconds,
    make_guid,
    quote_always,
    quote_text,
    seconds_to_ticks,
    ticks_to_seconds,
)

__all__ = [
    "AudioSource",
    "DEFAULT_GRID_LINE",
    "Envelope",
    "EnvelopePoint",
    "FxPlugin",
    "GUID_NAMESPACE",
    "Item",
    "Marker",
    "MidiEvent",
    "MidiSource",
    "Project",
    "Region",
    "Send",
    "StretchMarker",
    "Track",
    "format_exact",
    "format_hex",
    "format_number",
    "format_seconds",
    "make_guid",
    "quote_always",
    "quote_text",
    "seconds_to_ticks",
    "ticks_to_seconds",
]
