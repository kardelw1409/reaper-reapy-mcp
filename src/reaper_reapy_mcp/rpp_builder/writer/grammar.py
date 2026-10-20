"""THE grammar table: model field -> token -> number format.

Sources are named by line where the value is a constant, so every constant can
be re-checked against the reference without reading this file's history:

* `aligned_v4/.../dnb-neurofunk-174.rpp` lines 1-120 for the project block;
* the same file lines 121-197 for the track fields;
* the same file lines 146-171 for the item fields;
* the same file lines 320-395 for the MIDI source.

What is NOT in this table is as important as what is: folders, sends, FX and
envelopes have measured encodings (`RPP_FORMAT.md` section 9) but arrive in F5,
so the F2 validator refuses them instead of writing half a feature.
"""

from __future__ import annotations

from ..model.project import DEFAULT_GRID_LINE

#: Root opening line args (reference line 1). The third number grows on every
#: REAPER save (measured: 1792456158 then 1792483745..1792483992 across the six
#: F0-B saves), so it is VOLATILE: the verification protocol ignores it, and a
#: fixed value is what makes two builds byte-identical (`PLAN` section 10, item 4).
ROOT_TAG = "REAPER_PROJECT"
ROOT_ARGS: tuple[str, ...] = ("0.1", '"7.53/win64"', "1792456158", "0")

#: Project settings, in file order, between `<NOTES` and `TEMPO`
#: (reference lines 4-68). Each entry is `("field", token, args)` or
#: `("block", tag, args, children)`, where a child is the same shape.
HeaderItem = tuple

PROJECT_HEAD_ITEMS: tuple[HeaderItem, ...] = (
    ("field", "RIPPLE", ("0", "0")),
    ("field", "GROUPOVERRIDE", ("0", "0", "0", "0")),
    ("field", "AUTOXFADE", ("129",)),
    ("field", "ENVATTACH", ("3",)),
    ("field", "POOLEDENVATTACH", ("0",)),
    ("field", "TCPUIFLAGS", ("0",)),
    ("field", "MIXERUIFLAGS", ("11", "48")),
    ("field", "ENVFADESZ10", ("40",)),
    ("field", "PEAKGAIN", ("1",)),
    ("field", "FEEDBACK", ("0",)),
    ("field", "PANLAW", ("1",)),
    ("field", "PROJOFFS", ("0", "0", "0")),
    ("field", "MAXPROJLEN", ("0", "0")),
    ("field", "GRID", DEFAULT_GRID_LINE),  # substituted from Project.grid_line
    ("field", "TIMEMODE", ("1", "5", "-1", "30", "0", "0", "-1")),
    ("field", "VIDEO_CONFIG", ("0", "0", "65792")),
    ("field", "PANMODE", ("3",)),
    ("field", "PANLAWFLAGS", ("3",)),
    # Volatile in the reference (17 decimals, the edit cursor). Fixed at 0:
    # the protocol ignores it and a cursor position is not part of a build.
    ("field", "CURSOR", ("0",)),
    ("field", "ZOOM", ("100", "38138", "0")),
    ("field", "VZOOMEX", ("6", "0")),
    ("field", "USE_REC_CFG", ("0",)),
    ("field", "RECMODE", ("1",)),
    ("field", "SMPTESYNC", ("0", "30", "100", "40", "1000", "300", "0", "0", "1", "0", "0")),
    ("field", "LOOP", ("0",)),
    ("field", "LOOPGRAN", ("0", "4")),
    ("field", "RECORD_PATH", ('"Media"', '""')),
    ("block", "RECORD_CFG", (), (("field", "ZXZhdxgAAQ==", ()),)),
    ("block", "APPLYFX_CFG", (), ()),
    ("field", "RENDER_FILE", ('""',)),
    ("field", "RENDER_PATTERN", ('""',)),
    ("field", "RENDER_FMT", ("0", "2", "0")),
    ("field", "RENDER_1X", ("0",)),
    ("field", "RENDER_RANGE", ("1", "0", "0", "0", "1000")),
    ("field", "RENDER_RESAMPLE", ("3", "0", "1")),
    ("field", "RENDER_ADDTOPROJ", ("0",)),
    ("field", "RENDER_STEMS", ("0",)),
    ("field", "RENDER_DITHER", ("0",)),
    ("field", "RENDER_TRIM", ("0.000001", "0.000001", "0", "0")),
    ("field", "TIMELOCKMODE", ("1",)),
    ("field", "TEMPOENVLOCKMODE", ("1",)),
    ("field", "ITEMMIX", ("1",)),
    ("field", "DEFPITCHMODE", ("589824", "0")),
    ("field", "TAKELANE", ("1",)),
    ("field", "SAMPLERATE", ("44100", "0", "0")),  # substituted from Project.sample_rate
    ("block", "RENDER_CFG", (), (("field", "ZXZhdxgAAQ==", ()),)),
    ("field", "LOCK", ("1",)),
    (
        "block",
        "METRONOME",
        ("6", "2"),
        (
            ("field", "VOL", ("0.25", "0.125")),
            ("field", "BEATLEN", ("4",)),
            ("field", "FREQ", ("1760", "880", "1")),
            ("field", "SAMPLES", ('""', '""', '""', '""')),
            ("field", "SPLIGNORE", ("0", "0")),
            ("field", "SPLDEF", ("2", "660", '""', "0", '""')),
            ("field", "SPLDEF", ("3", "440", '""', "0", '""')),
            ("field", "PATTERN", ("0", "169")),
            ("field", "PATTERNSTR", ("ABBB",)),
            ("field", "MULT", ("1",)),
        ),
    ),
    ("field", "GLOBAL_AUTO", ("-1",)),
)

#: Master settings after `PLAYRATE`, before the master envelopes (lines 71-84).
#: `SELECTION`/`SELECTION2` are volatile (ignore list in `PLAN` section 6) and
#: fixed at zero.
PROJECT_MASTER_ITEMS: tuple[HeaderItem, ...] = (
    ("field", "SELECTION", ("0", "0")),
    ("field", "SELECTION2", ("0", "0")),
    ("field", "MASTERAUTOMODE", ("0",)),
    ("field", "MASTERTRACKHEIGHT", ("0", "0")),
    ("field", "MASTERPEAKCOL", ("16576",)),
    ("field", "MASTERMUTESOLO", ("0",)),
    ("field", "MASTERTRACKVIEW", ("0", "0.6667", "0.5", "0.5", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "1")),
    ("field", "MASTERHWOUT", ("0", "0", "1", "0", "0", "0", "0", "-1")),
    ("field", "MASTER_NCH", ("2", "2")),
    # MASTER_VOLUME is written from Project.master_volume / master_pan:
    # `<volume> <pan> -1 -1 1` - the same shape as a track VOLPAN (line 80).
    ("field", "MASTER_VOLUME", ()),
    ("field", "MASTER_PANMODE", ("3",)),
    ("field", "MASTER_PANLAWFLAGS", ("3",)),
    ("field", "MASTER_FX", ("1",)),
    ("field", "MASTER_SEL", ("0",)),
)

#: Every project field the writer emits, so the reader can tell an unknown
#: field from a constant it already reproduces.
PROJECT_KNOWN_TOKENS: frozenset[str] = frozenset(
    [item[1] for item in PROJECT_HEAD_ITEMS if item[0] == "field"]
    + [item[1] for item in PROJECT_MASTER_ITEMS]
    + ["TEMPO", "PLAYRATE", "GLOBAL_AUTO"]
)

#: Track fields, in measured order (reference lines 122-145). Values marked
#: `None` are substituted by the writer from the model; everything else is a
#: constant measured on all 14 tracks.
TRACK_FIELD_ORDER: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("NAME", None),
    ("PEAKCOL", None),
    ("BEAT", ("-1",)),
    ("AUTOMODE", ("0",)),
    ("PANLAWFLAGS", ("3",)),
    ("VOLPAN", None),
    ("MUTESOLO", None),
    ("IPHASE", ("0",)),
    ("PLAYOFFS", ("0", "1")),
    ("ISBUS", None),
    ("BUSCOMP", None),
    ("SHOWINMIX", ("1", "0.6667", "0.5", "1", "0.5", "0", "0", "0", "0")),
    ("FIXEDLANES", ("9", "0", "0", "0", "0")),
    ("SEL", ("0",)),
    ("REC", ("0", "0", "1", "0", "0", "0", "0", "0")),
    ("VU", ("64",)),
    ("TRACKHEIGHT", ("0", "0", "0", "0", "0", "0", "0")),
    ("INQ", ("0", "0", "0", "0.5", "100", "0", "0", "100")),
    ("NCHAN", None),
    ("FX", ("1",)),
    ("TRACKID", None),
    ("PERF", ("0",)),
    ("MIDIOUT", ("-1",)),
    ("MAINSEND", ("1", "0")),
)

#: Measured defaults used when the model leaves a field at its default.
#: `PEAKCOL 16576` is the reference colour; `FX 1` is a FLAG BYTE, not "FX on".
DEFAULT_PEAKCOL = "16576"

#: Item fields, in measured order (reference lines 147-167). `None` means the
#: writer substitutes a model value.
ITEM_FIELD_ORDER: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("POSITION", None),
    ("SNAPOFFS", ("0",)),
    ("LENGTH", None),
    ("LOOP", None),
    ("ALLTAKES", ("0",)),
    ("FADEIN", ("1", "0", "0", "1", "0", "0", "0")),
    ("FADEOUT", ("1", "0", "0", "1", "0", "0", "0")),
    ("MUTE", ("0", "0")),
    ("SEL", None),
    ("IGUID", None),
    ("IID", None),
    ("NAME", None),
    ("VOLPAN", ("1", "0", "1", "-1")),
    ("SOFFS", None),
    ("PLAYRATE", ("1", "1", "0", "-1", "0", "0.0025")),
    ("CHANMODE", ("0",)),
    ("GUID", None),
)

#: Fixed MIDI source tail (reference lines 389-394). Written after the events.
MIDI_SOURCE_TAIL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CCINTERP", ("32",)),
    ("CHASE_CC_TAKEOFFS", ("1",)),
    ("IGNTEMPO", ("0", "120", "4", "4")),
    ("EVTFILTER", ("0", "-1", "-1", "-1", "-1", "0", "0", "0", "0", "-1", "-1", "-1", "-1", "0", "-1", "0", "-1", "-1")),
)

#: Indentation: measured 2 spaces per depth level (RPP_FORMAT.md section 1).
INDENT = "  "
