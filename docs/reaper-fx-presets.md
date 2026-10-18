# REAPER's own FX presets - the fast channel (`get_fx_presets` / `set_fx_preset` / `navigate_fx_presets` / `save_fx_preset`)

**What this is.** REAPER keeps its own preset list per FX (the dropdown in the FX window). It is a
*different store* from the plugin's built-in browser that `plugin_presets` serves, and it is the fast
one: applying a preset is **one host call** that lets the plugin load its own state - no track-chunk
read, no chunk write, and therefore no state-size ceiling.

**Measured on this machine (2026-10-18).** `%REAPER%\presets\vst3-Vital.ini` - 3,912,003 bytes, 140
lines, 4 presets (`BassHouse` 175,762 B, `pad` 176,148 B, `DirtyBassKongo` 1,428,230 B, `Bass 808`
175,065 B). Format, verified by reading the file (parser pinned by `tests/test_reaper_presets.py`):

```
[General]
NbPresets=4

[Preset0]
Data=<uppercase hex, 32,770 characters = 16,385 bytes>
Data_1=<...>
...
Len=175762
Name=BassHouse
```

* chunking: the payload is split every **16,384 bytes**, and each part is stored as hex of the part
  **plus one checksum byte** (`sum(part) & 0xFF`) - 16,385 bytes = 32,770 hex characters per full
  line, keys `Data`, `Data_1`, ...; the last one is short;
* `Len` = payload size in **bytes** (so a parse is checkable without decoding: the stored hex is
  `2 * (Len + number of chunks)`);
* line endings are **CRLF** (measured: 140 CRLF, 0 lone LF), and the file is written back that way;
* the decoded payload reaches the ASCII `VstW` marker at ~offset 48 - the same wrapper the plugin's
  state carries inside REAPER's track chunk, i.e. the file stores the FX's own VST3 state.

**How the layout was proved** (not assumed): joining the chunks raw, or dropping their FIRST byte,
corrupts the plugin document at every boundary - only "drop the last byte of each chunk" decodes to
JSON with a `settings` object (781 keys, the same count the plugin layer reads live). With that
model the reassembled payload's `Len` matches exactly for all four presets, and every chunk's last
byte equals `sum(part) & 0xFF` (checked for all of them; three were verified by hand first:
`0x87`, `0xcb`, `0x19`). On the real file the parser now reports **zero problems** and all four
presets pass `checksums_ok` and `len_ok`; a simulated append leaves every existing byte identical
and touches only the `NbPresets` line plus the new section.

**What the host API offers** (enumerated in `reapy.reascript_api`): `TrackFX_GetPreset`,
`TrackFX_GetPresetIndex`, `TrackFX_SetPreset`, `TrackFX_SetPresetByIndex`, `TrackFX_NavigatePresets`,
`TrackFX_GetUserPresetFilename` - and **no save call at all**. Saving is therefore done by writing
REAPER's own file in REAPER's own format, and then **the host has to confirm it** (see below).

## The four tools

| tool | what it does | cost |
|---|---|---|
| `get_fx_presets(track_index, fx_index)` | current preset, host count + index, the user preset file, the names parsed from that file, and the integrity of the CURRENT preset's payload | host reads only; the file is parsed once |
| `set_fx_preset(..., name=`*or*`index)` | apply one preset from REAPER's set, verified by read-back | one host call |
| `navigate_fx_presets(..., step)` | step through the set (`step` may be negative), reporting `before`/`after`/`changed` | one host call |
| `save_fx_preset(..., name, dry_run=True)` | save the FX's CURRENT state as a REAPER preset, host-verified | one chunk read + one file write |

Honest edges, all reported rather than hidden:

* a plugin that manages its own presets exposes none here - the answer carries `channel_empty`
  (measured earlier in the plugin layer: Vital's VST3 program channel answers empty/`-1`; the file
  above exists because presets were saved **through REAPER**, which is why this channel is the useful
  one);
* when the host's count and the file's names disagree, the answer carries `disagreement` and says
  which selector uses which numbering (index → the host, name → the file);
* `set_fx_preset` requires exactly one of `name`/`index`: a call naming both would have to pick one
  silently;
* `verified` is only true when the selector BECAME current - a host that answers `true` without
  changing the preset is reported as not verified.

## Saving: why it is verified

An unconfirmed preset is a trap (it looks saved and does not load), so `save_fx_preset` runs a chain
and refuses at the first step that does not hold:

1. the FX's state is read from the track chunk (item A + item B, the bytes a preset stores);
2. the section is appended in REAPER's format, and the file is backed up **once** beside itself as
   `<file>.reaper-mcp-backup` (never overwritten, so the way back does not depend on this call);
3. `dry_run` stops here and returns the plan;
4. otherwise the file is written and the HOST must confirm: the preset count grows, the new name
   becomes selectable (`TrackFX_SetPreset`), and the state that comes back after applying it equals
   the bytes that were saved;
5. any failure restores the file, re-applies the preset that was current, and answers
   `status: error` with the numbers it measured.

`append_preset` refuses to touch a file that does not match the measured format: appending to
something REAPER would then misread is worse than not saving.
