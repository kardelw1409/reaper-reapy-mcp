"""Numbers, identifiers and text tokens in the form REAPER writes them.

Every rule here is measured on the references, not assumed (`Docs/RPP_FORMAT.md`
sections 0 and 8):

* a value is written with at most 6 decimals and trailing zeros are dropped -
  `25.12058`, `0`, `13.289274`, `0.047206`;
* a text value containing a space is quoted (`NAME "Ins - ins.mid"`), an empty
  one is `""`, and a value without a space is written bare (`NAME song.wav`);
* an identifier is `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}` in upper case.

Two things are deliberately NOT generalised:

1. `format_exact()` keeps the shortest round-trip representation of a float.
   Only `SOFFS2` uses it, because REAPER stores more than 6 decimals there.
   The measured arithmetic is `SOFFS1 * (bpm / 60.0)`: the algebraically
   equivalent `SOFFS1 * bpm / 60.0` produces `68.0000012091` where the file
   holds `68.00000120909999`, i.e. a different double.
2. `quote()` refuses a value containing a double quote: REAPER has no escape
   sequence measured for it, so writing one would be a guess.
"""

from __future__ import annotations

import uuid

#: Namespace for deterministic GUIDs (decision 4: two runs produce one file).
#: A constant, not a random namespace: changing it changes every generated id.
GUID_NAMESPACE = uuid.UUID("0c9a1f47-5b3e-4d6a-9f22-8e1d7c4a6b10")

#: The decimal precision REAPER keeps for positions, lengths and gains
#: (RPP_FORMAT.md section 8).
MAX_DECIMALS = 6


def make_guid(*parts: object) -> str:
    """A deterministic identifier in the form REAPER writes: `{UPPER-CASE}`.

    The same parts always give the same id, on any machine and any Python run,
    which is what makes two builds of one project byte-identical.
    """
    name = "\x1f".join(str(part) for part in parts)
    return "{%s}" % str(uuid.uuid5(GUID_NAMESPACE, name)).upper()


def format_number(value: float, decimals: int = MAX_DECIMALS) -> str:
    """Format a number the way REAPER writes it: fixed decimals, zeros trimmed.

    `0.0` -> `0`, `25.12058` -> `25.12058`, `13.289274` -> `13.289274`.
    Negative zero is normalised to `0`, because `-0.000000` is not a value
    REAPER has ever been measured writing.
    """
    text = ("%." + str(int(decimals)) + "f") % float(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def format_exact(value: float) -> str:
    """Shortest round-trip float text, without a trailing `.0`.

    Used only where REAPER itself keeps more than 6 decimals (MIDI `SOFFS2`);
    do not use it for positions - `format_number()` is the measured style.
    """
    text = repr(float(value))
    if text.endswith(".0"):
        text = text[:-2]
    return text


def format_seconds(value: float) -> str:
    """A time in seconds: 6 decimals, trimmed (RPP_FORMAT.md section 8)."""
    return format_number(value)


def quote_text(text: str) -> str:
    """The token for a text value: bare, or quoted when it contains a space.

    Measured: `NAME AI_MASTER` is bare, `NAME "Ins - ins.mid"` is quoted,
    `NAME ""` is the empty value (RPP_FORMAT.md section 8).
    """
    if '"' in text:
        raise ValueError(
            "value contains a double quote and REAPER has no measured escape for it: %r"
            % (text,)
        )
    if text == "":
        return '""'
    if any(char.isspace() for char in text):
        return '"%s"' % text
    return text


def quote_always(text: str) -> str:
    """The token for a value REAPER always quotes (measured on `FILE`)."""
    if '"' in text:
        raise ValueError(
            "value contains a double quote and REAPER has no measured escape for it: %r"
            % (text,)
        )
    return '"%s"' % text


def seconds_to_ticks(seconds: float, ppq: int, bpm: float) -> int:
    """Seconds -> ticks: `t = s * ppq * bpm / 60` (RPP_FORMAT.md section 7.2)."""
    return int(round(float(seconds) * int(ppq) * float(bpm) / 60.0))


def ticks_to_seconds(ticks: int, ppq: int, bpm: float) -> float:
    """Ticks -> seconds: `s = ticks / ppq * 60 / bpm`."""
    return float(ticks) / int(ppq) * 60.0 / float(bpm)


def format_tick_delta(delta: int) -> str:
    """A MIDI delta: decimal ticks (the `E` line mixes bases on purpose)."""
    return str(int(delta))


def format_hex(value: int) -> str:
    """MIDI status/data byte: lower-case hex, ALWAYS two digits.

    Measured on all 4405 `E` lines of the reference: status, data1 and data2 are
    exactly two characters each (`00`, `3c`, `7b`, `b0`). The earlier reading
    "no leading zeros" came from examples whose bytes all happened to be >= 0x10;
    REAPER's own resave of a zero byte proved it wrong (it wrote `00`).
    """
    return "%02x" % int(value)
