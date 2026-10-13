"""Independent verification of the v3 tab + a full tab inventory."""

from __future__ import annotations

from pathlib import Path

import mido
import reapy
from reapy import reascript_api as RPR

BUNDLE = Path(
    r"C:\repos\mcp-audiocpp\work\20261013-091330_bundle-house-afterglow-v2"
)

reapy.connect()
project = reapy.Project()
print("=== CURRENT TAB ===")
print("id:", repr(project.id), "| tracks:", len(project.tracks), "| bpm:", project.bpm)

bar = 4 * 60 / 128.0
print(f"bar = {bar} s")
for track in project.tracks:
    for index in range(track.n_items):
        item = RPR.GetTrackMediaItem(track.id, index)
        take = RPR.GetActiveTake(item)
        pos = float(RPR.GetMediaItemInfo_Value(item, "D_POSITION"))
        length = float(RPR.GetMediaItemInfo_Value(item, "D_LENGTH"))
        offset = float(RPR.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS"))
        notes = RPR.MIDI_CountEvts(take, 0, 0, 0)
        note_count = notes[2] if isinstance(notes, list) and len(notes) > 2 else 0
        audible_from = pos + (offset if offset else 0.0)
        bars = audible_from / bar
        print(
            f"{str(track.name):12s} pos={pos:7.3f} len={length:7.3f} take_off={offset:6.3f} "
            f"audible_from={audible_from:7.3f} = bar {bars:6.2f} | midi_notes={note_count}"
        )

print("=== REGIONS (project time) ===")
index = 0
while True:
    row = RPR.EnumProjectMarkers2(project.id, index, 0, 0.0, 0.0, "", 0)
    if not row[0]:
        break
    if row[3]:
        start, end = float(row[4]), float(row[5])
        print(f"  {start:8.3f} .. {end:8.3f}  (bar {start / bar:6.2f} .. {end / bar:6.2f})")
    index += 1

print("=== BUNDLE MIDI first notes (project time) ===")
for name in ("drums.mid", "bass.mid", "vocal.mid", "ins.mid"):
    path = BUNDLE / name
    midi = mido.MidiFile(str(path))
    tempo = next((m.tempo for t in midi.tracks for m in t if m.type == "set_tempo"), 500000)
    first = None
    bars = []
    for track in midi.tracks:
        absolute = 0
        for message in track:
            absolute += message.time
            if message.type == "note_on" and message.velocity > 0:
                seconds = absolute * (tempo / 1e6) / midi.ticks_per_beat
                if first is None or seconds < first:
                    first = seconds
                bars.append(seconds / bar)
    if first is not None:
        print(f"  {name:10s} first note {first:7.3f} s = bar {first / bar:6.2f}")

print("=== ALL TABS ===")
zero = "(ReaProject*)0x0000000000000000"
i = 0
while True:
    row = RPR.EnumProjects(i, "", 512)
    if not row[0] or row[0] == zero:
        break
    other = reapy.Project(row[0])
    names = [str(t.name) for t in other.tracks]
    print(f"  tab {i}: {row[0]} tracks={len(other.tracks)} bpm={other.bpm} first={names[:3]}")
    i += 1
