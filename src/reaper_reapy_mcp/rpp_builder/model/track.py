"""Tracks, sends and FX plugins.

Grammar: `Docs/RPP_FORMAT.md` sections 4 (track fields), 9.1 (`ISBUS`/`BUSCOMP`
folder encoding) and 9.2 (`AUXRECV` written on the RECEIVING track).

The model is a SUPERSET of what F2 writes (`PLAN` section 5, rule 1): folders,
sends and FX are fields here from the start, and the F2 validator refuses
non-default values because their writer lines arrive in F5. Refusing loudly is
the only honest option - silently dropping a folder would change the project.
"""

from __future__ import annotations

from dataclasses import dataclass

from .envelope import Envelope
from .item import Item


@dataclass(frozen=True, slots=True)
class Send:
    """One `AUXRECV` line on the receiving track (F5)."""

    source_track: int  # 0-based track index
    volume: float = 1.0  # linear
    channels: str = "-1:U"


@dataclass(frozen=True, slots=True)
class FxPlugin:
    """One plugin inside `<FXCHAIN` (F5).

    The state is opaque base64: it is carried, never synthesised.
    """

    ident_line: str
    state_b64: tuple[str, ...] = ()
    fxid: str | None = None
    bypass: tuple[int, int, int] = (0, 0, 0)


@dataclass(slots=True)
class Track:
    """One `<TRACK` block.

    Measured defaults are the reference's own values (`RPP_FORMAT.md` section 4):
    `PEAKCOL 16576`, `VOLPAN 1 0 -1 -1 1`, `ISBUS 0 0`, `BUSCOMP 0 0 0 0 0`,
    `NCHAN 2`, `MAINSEND 1 0`. Any other value is written from the field.
    """

    name: str
    kind: str = "audio"  # "audio" | "midi" | "folder" | "bus" (informational)
    guid: str | None = None
    color: int = 16576  # PEAKCOL: the reference colour on all 14 tracks (line 123)
    volume: float = 1.0
    pan: float = 0.0
    mute: bool = False
    solo: bool = False
    channels: int = 2
    folder_kind: int = 0  # ISBUS[0]: 0 plain, 1 opens, 2 closes (F5)
    folder_depth: int = 0  # ISBUS[1]: +1 on an opener, -1 on a closer (F5)
    folder_compact: int = 0  # BUSCOMP[0]: 0 normal, 1 small, 2 tiny (F5)
    sends: tuple[Send, ...] = ()  # F5
    fx: tuple[FxPlugin, ...] = ()  # F5
    envelopes: tuple[Envelope, ...] = ()  # F5
    items: tuple[Item, ...] = ()
    #: Unknown fields, in file order: `((token, (arg, ...)), ...)`.
    extra: tuple[tuple[str, tuple[str, ...]], ...] = ()
