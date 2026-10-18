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
* the decoded payload reaches the ASCII `VstW` marker at offset **52** - the same wrapper the plugin's
  state carries inside REAPER's track chunk, i.e. the file stores the FX's own VST3 state (an
  earlier note said 48: it came from a truncated hex dump and was wrong; the tests pin 52 and the
  plugin document's start at 228 against the real file and the live block).

**How the layout was proved** (not assumed): joining the chunks raw, or dropping their FIRST byte,
corrupts the plugin document at every boundary - only "drop the last byte of each chunk" decodes to
JSON with a `settings` object (781 keys, the same count the plugin layer reads live). With that
model the reassembled payload's `Len` matches exactly for all four presets, and every chunk's last
byte equals `sum(part) & 0xFF` (checked for all of them; three were verified by hand first:
`0x87`, `0xcb`, `0x19`). On the real file the parser now reports **zero problems** and all four
presets pass `checksums_ok` and `len_ok`; a simulated append leaves every existing byte identical
and touches only the `NbPresets` line plus the new section.

## The blob also carries the NAME, and that is what a save has to write

The payload is not "the plugin's state": it is `item A (44 bytes) + item B + NUL + <name> + five
NULs`, and `Len == 44 + A[0x20] + len(name) + 6`. Measured on every preset of the real file:

| preset | `Len` | `A[0x20]` | `44 + A[0x20]` | trailer | name read back |
|---|---|---|---|---|---|
| `BassHouse` | 175,762 | 175,703 | 175,747 | 15 | `BassHouse` (9 + 6) |
| `pad` | 176,148 | 176,095 | 176,139 | 9 | `pad` (3 + 6) |
| `DirtyBassKongo` | 1,428,230 | 1,428,166 | 1,428,210 | 20 | `DirtyBassKongo` (14 + 6) |
| `Bass 808` | 175,065 | 175,007 | 175,051 | 14 | `Bass 808` (8 + 6) |

So a blob built as "item A + item B" is **shorter than anything REAPER itself produced**, and a
`Len` that does not account for the trailer is wrong. `split_preset_blob` splits a blob into the
state and the trailer, `read_name_trailer` decodes the name (returning `None` for a region that is
not the measured shape - "not understood", never a guess), and `preset_blob(state, name)` writes the
trailer fresh, so re-saving under a new name cannot keep the old one. Checked against the real file:
all four relations hold, every name reads back, and rebuilding each blob under a new name leaves the
state byte-for-byte identical.

**One correction to an earlier reading:** the `VstW` marker sits at offset **52**, not 48. The 48
came from a truncated hex dump (48 bytes of hex printed, `VstW` immediately after) and was wrong in
both directions - the real file and the live block shape agree on 52, which is what the test now
pins, together with the plugin document starting at 228 in both.

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

* a plugin that manages its own presets exposes none here - with NO preset selected the measured
  answer is: retval `1`, an EMPTY name buffer (`current: null`), `preset_index: -1` - while
  `host_preset_count` is real (`4` on Vital). The channel is alive and empty, not dead; P38's
  "answers empty/`-1`" measurement is about the plugin's VST3 *program* channel, and the file above
  exists because presets were saved **through REAPER**, which is why this channel is the useful one;
* when the host's count and the file's names disagree, the answer carries `disagreement` and says
  which selector uses which numbering (index -> the host, name -> the file);
* `set_fx_preset` requires exactly one of `name`/`index`: a call naming both would have to pick one
  silently;
* `verified` is only true when the selector BECAME current - a host that answers `true` without
  changing the preset is reported as not verified;
* the answers carry the container the host actually returned (`read_shapes`, `call_layout`, e.g.
  `list[int,echo,int,str,int]`): reapy's distributed API returns the WHOLE argument list with
  out-parameters updated in place and `retval` FIRST, and `TrackFX_SetPreset` (no out-parameters)
  answers the bare `1` - a `bool(list)` reading of it would be true forever (all measured live, P43).

## The settling law of an applied preset (measured live, P43)

Byte-identity between a saved blob and the state the plugin shows after applying it is **not
achievable on this plugin**, and the file's own `BassHouse` behaves exactly like one this layer
writes. Measured by applying the preset three times and reading the state out of the track chunk:

| application | state | what it holds |
|---|---|---|
| #1 | 175,761 bytes | the preset, plus the LIVE runtime sampler values (`sample_on` 1.0 vs stored 0.0, `sample_level` 0.85 vs 0.7071067690849304, `loop` 0 vs 1, `destination` 4 vs 3) |
| #2 | 175,747 bytes = the stored core's own length | the stored values |
| #3 | byte-identical to #2 (`sha256 8f6a920e62fab826`) | a fixed point |

After #2 the decoded document differs from the file in ONE key of 781 (`settings.sample.samples`,
the base64 PCM of the preset's own sample): same length (117,600 characters), first difference at
character 11,221 (~4 int16 samples of shift). So the verified chain is: count grows -> the name
becomes current -> TWO applications reach a fixed point -> that point has the same LENGTH as the
bytes saved; the byte difference is reported with its numbers (`verification.byte_difference`),
never asserted away, and the way back applies the previous preset twice for the same reason.

## Saving: why it is verified

An unconfirmed preset is a trap (it looks saved and does not load), so `save_fx_preset` runs a chain
and refuses at the first step that does not hold:

1. the FX's state is read from the track chunk (item A + item B, the bytes a preset stores);
2. the blob is completed with REAPER's own name trailer (`NUL + name + five NULs`) - measured on the
   real file, see above - and the section is appended in REAPER's format, with the file backed up
   **once** beside itself as `<file>.reaper-mcp-backup` (never overwritten, so the way back does not
   depend on this call);
3. `dry_run` stops here and returns the plan;
4. otherwise the file is written and the HOST must confirm: the preset count grows, the new name
   becomes current, and the state the saved bytes drive the plugin to is a **fixed point** (two
   applications reach it; a third changes nothing) of the **same length** as the bytes that were
   saved - byte-identity is NOT required (see the settling law above) and the difference goes into
   `verification.byte_difference` with its numbers;
5. any failure restores the file, re-applies the preset that was current (twice, for the same
   settling reason), and answers `status: error` with the numbers it measured.

`append_preset` refuses to touch a file that does not match the measured format: appending to
something REAPER would then misread is worse than not saving.
