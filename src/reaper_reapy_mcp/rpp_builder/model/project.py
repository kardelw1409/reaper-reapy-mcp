"""Project-level model: the root block, markers and regions.

Grammar: `Docs/RPP_FORMAT.md` sections 2 (project block) and 3 (markers and
regions).

`Project.title` is NOT a file field in any measured reference: the title lived
in a marker the live fill created, and the root block has no title token. It is
kept as metadata (reports, deterministic GUID seeds) and is never invented into
the output - a reader takes it from the caller, e.g. the file name.

`Project.grid_line` is stored as RAW TOKENS on purpose. The `GRID` line is
`3199 8 1 8 1 0 0 0` in all seven references, including projects whose drawn
grid differs, so the meaning of its tokens is NOT decoded. Carrying the tokens
verbatim is measured; deriving them from a subdivision would be a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .track import Track

#: The measured `GRID` line, identical in all seven references (RPP_FORMAT §2/§9.0).
DEFAULT_GRID_LINE: tuple[str, ...] = ("3199", "8", "1", "8", "1", "0", "0", "0")


@dataclass(frozen=True, slots=True)
class Marker:
    """A plain marker: one 10-token `MARKER` line (RPP_FORMAT section 3)."""

    name: str
    pos_s: float
    guid: str | None = None


@dataclass(frozen=True, slots=True)
class Region:
    """A region: an opening `MARKER` line plus a closing one sharing its id."""

    name: str
    start_s: float
    end_s: float
    guid: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    """One `<REAPER_PROJECT>` block.

    Defaults are the reference's measured values. `notes` is the text INSIDE the
    `<NOTES 0 2` block; the empty string reproduces the empty block.
    """

    title: str
    tempo_bpm: float
    meter: tuple[int, int] = (4, 4)
    sample_rate: int = 44100
    grid_line: tuple[str, ...] = DEFAULT_GRID_LINE
    master_volume: float = 1.0
    master_pan: float = 0.0
    tracks: tuple[Track, ...] = ()
    markers: tuple[Marker, ...] = ()
    regions: tuple[Region, ...] = ()
    notes: str = ""
    #: Unknown project fields, in file order: `((token, (arg, ...)), ...)`.
    extra: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())
