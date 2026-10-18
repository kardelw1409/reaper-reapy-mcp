# `vision_*` — looking at REAPER (contract + measurements)

Six MCP tools that let an agent **see** the REAPER window and move its view. They
exist because project data cannot answer "what does it look like": a screenshot
with a known time range is what a review of an arrangement, a region layout or a
mix needs, and a human was taking those screenshots by hand before this.

Implementation: `src/reaper_reapy_mcp/vision/`. Offline tests: `tests/vision/`.
A REAPER-side CLI with the same code: `Scripts/reaper_vision/reaper_shot.py`
(shim over `python -m reaper_reapy_mcp.vision.cli`).

## Tools

| tool | what it does | image |
|---|---|---|
| `vision_state` | window rect/minimized/foreground, visible range, zoom px/s, tempo, tracks, length, metre, write counters, image directory | no |
| `vision_shot` | screenshot of the whole REAPER window | yes |
| `vision_view` | show an exact time range, or shift the current one | with `shot=true` |
| `vision_scroll` | scroll horizontally (`dx`) / vertically (`dy`) | with `shot=true` |
| `vision_zoom` | horizontal zoom (exact factor, or steps) / vertical steps | with `shot=true` |
| `vision_scan` | tile the arrangement into time slices, one captioned contact sheet | yes (sheet) |

Every answer is the house envelope (`{"status": "success" | "error", ...}`, plus
`hints` on a refusal), and the image is returned as MCP `ImageContent`. No
argument is required: a call with no arguments is always valid input (the
refusals explain what to give).

## What was measured, and why the code looks like this

All of it on this machine, 2026-10-17/18, REAPER 7.53. The rules that matter are
enforced by tests in `tests/vision/`, so they cannot be silently relaxed.

### The frame is the window's own rendering, not a screen grab

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` on `GetMainHwnd` renders REAPER's
content **while another application covers it**:

| comparison | mean grey difference (0-255) | samples differing by >32 |
|---|---|---|
| occluded render vs screen with REAPER activated | **8.1** | 5.9 % |
| **control:** occluded render vs screen showing the covering app | 63.5 | 71.8 % |

The control is the point: the comparison *can* fail, so the 8.1 is a statement
about content. A `BitBlt` screen grab is kept in the code as that control only —
it returns the covering application (it returned VS Code while REAPER was the
target).

### A minimized window is recovered, not photographed

A minimized window renders a **237×39 / 4-colour artefact**. `looks_like_window`
rejects it, a rejected frame is reported with its numbers and **no image is sent**
(so nothing is read from an artefact), and recovery uses
`ShowWindow(SW_SHOWNOACTIVATE)`: measured to work, to stay recovered (re-checked
after 1 s and 3 s), and to leave focus alone. `SW_RESTORE` is **not** used — it
returns a *maximized* window to its 683×512 normal size. The window is put back
the way it was found, and `window.*` in the answer says what happened.

### A frame is only reported once it is fresh

A view change paints asynchronously, so two consecutive identical frames are
required (`settle`). An unsettled frame is reported as `settled: false` rather
than presented as fresh — the alternative is an agent reasoning about a picture
of the previous bar.

### The span is exact, the position is quantised — both are reported

`GetSet_ArrangeView2(isSet=1)` honours the **span** exactly (0.0 s error in every
measured case) and quantises the **position** with a step that grows with the
span: 8.0 ms at a 31.5 s span, 79.5 ms at a 294 s span — 2.7 × 10⁻⁴ of the span
both times. Repeatable for the same request, independent of zoom. A second write
of the returned value converges in only 4 of 6 cases, so convergence is never
claimed. Every answer therefore carries `start_error_s`, `end_error_s`,
`tolerance_s` and `tolerance_rule`.

`restore_view` is the deliberate exception: writing back a range the host itself
produced round-trips **exactly** (10⁻¹² s), so a restore is checked with a
floating-point tolerance and a drift is reported instead of absorbed.

### Scrolling

`CSurf_OnScroll(dx, 0)` moves the view exactly **16 px per unit** (measured twice:
10/50/100/200 units on 2026-10-17, then 100 units → 299.2049 s at 5.3475 px/s
from inside the tool). It takes **two** arguments — three raises
`takes 2 positional arguments but 3 were given` inside REAPER, which looks like a
dead bridge. `CSurf_OnScroll(0, dy)` scrolls vertically, positive `dy` moving the
track list down; it **clamps** at the ends, so a magnitude is only ever reported
as measured from the image (`image_shift`), with `reliable: false` when the
correlation peak is not clearly better than the rest of the curve (repetitive
MIDI parts make several lags fit equally well).

### Actions are fired by NAME, never by id alone

`kbd_getTextFromCmd(id, 0)` is read first and must equal the expected string, or
the call refuses and reports both names — a re-mapped action list cannot be fired
into. The set: 1011/1012 horizontal zoom out/in, 40111/40112 vertical zoom
in/out, 40138/40139 page up/down, 40140/40141 page left/right. Vertical zoom is
exactly reversible: three steps in then three out reproduced the starting frame
pixel for pixel, which is why `vision_zoom(v=...)` verifies its own restore.

### The metre is read, not assumed

`--span-bars` used to assume 4/4. `TimeMap_GetTimeSigAtTime(proj, t, 0, 0, 0.0)`
**does** fill its out-params — `[proj, t, 2, 4, 150.1214]` — and the live project
displayed exactly "2/4" at 150.121 BPM. So a bar length is measured
(`numerator × 4/denominator × 60/tempo`, the denominator included because a 6/8
bar is three quarter-note beats, not six) and the answer says which it used
(`bar_source`, `bar_assumed`). A 4-bar slice of that project is 3.197 s, not the
6.4 s a 4/4 assumption would have produced.

### Looking cannot dirty the project

Every audited operation (set view, scroll, all eight page/zoom actions) left
`GetProjectStateChangeCount` and `Undo_CanUndo2` **unchanged** — delta 0, no undo
change. Each answer carries that witness (`state_change_delta`), and when a
counter cannot be read the value is `None`, never 0: 0 is the claim.

### An empty-looking project: REAPER's media-offline setting (the real cause)

This is the single most misleading thing about photographing REAPER's window. It was
reported by the user before it was measured (*"рипер подгружает аудио только когда
окно рипера активно"*) and finally **explained by the user**:

> **Preferences → Media → "Set media items offline when application is not active"**
> (`offlineinact` in `reaper.ini`: `1` = on, `0` = off)

With the setting **ON** - measured on a live host (2026-10-18, 6 audio sources + 8
MIDI takes):

| state | `GetMediaSourceLength` (mix) | sources open | waveform drawn |
| --- | --- | --- | --- |
| window in the BACKGROUND | **0.0 s**, 1 ch, 0 Hz | 0 of 6 | no |
| after activation + wait | **89.079 s**, 2 ch, 48000 Hz | 6 of 6 | yes |
| right after the focus is given back | 0.0 s again | 0 of 6 | no |

The file is on disk the whole time and the project names it; REAPER simply has not
*opened* it, so the item draws no waveform. The wait is ~1.1 s for one source and
**3.4-3.6 s** for six, driven by the host's own answer (`view.wait_for_media` polls
per-source lengths), never by a sleep.

With the setting **OFF** the whole problem disappears — measured:

* 6 of 6 sources stay open while REAPER is **in the background**;
* a frame taken with REAPER behind another window is **pixel-identical** to one
  taken while it is active (whole-window mean |delta| **0.000**; the waveform ink
  measured **9.06 %** in the background, active, and after a focus restore);
* a `vision_shot` with no activation returns `ok: true`, `state_change_delta: 0`
  and the same 9.06 % ink — so **no activation is ever needed**, and none happens by
  default.

    → THE ADVICE IS TO TURN THE SETTING OFF AND STOP ACTIVATING. Activating pulls
      the user's window in front of whatever they are doing, which is why it is a
      flag (`activate=false` by default) rather than behaviour.

HOW THE TOOLS HANDLE IT

* `vision_state` reports `media` (open / pending sources) **and** `media_settings`
  (`offline_when_inactive`, the raw ini value, the ini path) — so "why do the items
  look empty?" is answered without a picture, and the answer names the cause instead
  of the symptom. An unreadable setting is reported as **UNKNOWN**, never as "off":
  "off" is the claim that makes an empty item surprising;
* `vision_shot` / `vision_scan` take `activate` (**default false**) and
  `activate_timeout` / `restore_focus`; with `activate=true` the frame is taken
  *before* the focus goes back (the restore unloads the media when the setting is
  on), and the answer carries `activation.media_settings`, `media_before`,
  `media_after`, `focus_restored`, `media_after_restore` and a note naming the
  setting plus both remedies;
* activation itself uses the plain `SetForegroundWindow` first (refused by Windows'
  foreground lock when the caller is not foreground — measured to return False with
  no change) and then the `AttachThreadInput` chain. Minimize + restore also works
  and is **rejected**: it flickers the user's screen and permanently changes a
  maximized window's restore state. `ActiveWindowTracking` ("activate a window by
  hovering over it") is **off** on this machine, so the mouse being on another
  monitor does not take the focus.

HOW THE WAVEFORM IS RECOGNISED IN PIXELS (measured, this theme)

The item fill is grey **100-150** once the peaks are drawn, and the waveform is
**dark ink** on top of it (grey 20-38, ~9 % of the arrange area); grid lines are 14,
an empty TCP 38, the item's label strip 65 for its top 21 px, lane height 134 px.
An earlier rule looked for pixels *brighter* than the fill and found nothing —
which is how "the waveform is not drawn" was concluded while it was. The metric used
in the probes is the fraction of ink over a fixed arrange region, and it separates
the states cleanly: **9.06 % ink / 60.29 % fill** when waveforms are drawn against
**1.7 % / 0 %** when the sources are closed. (Pixels must be measured on the region
that actually holds the lanes: `I_TCPY`/`I_TCPH` are relative to the top of the
arrange view, not the window.)

### Traps that cost real time

* `GetMainHwnd` comes back through reapy as the **string**
  `'(HWND)0x00000000000107B0'`; `int()` on it raises.
* A Win32 call whose restype is a pointer type returns **`None`** for 0 (ctypes
  converts NULL to None), so `int(GetForegroundWindow())` raised
  `int() argument must be … not 'NoneType'` in the moment the desktop had no
  foreground window — an otherwise complete answer died on a detail it never
  needed.
* ctypes needs explicit prototypes for `GetDIBits` (else `int too long to
  convert` on a 64-bit HDC), and PIL refuses a negative-stride view with
  `tile cannot extend outside image`.
* In the shifted-frame correlation, `band_b[:rows + shift]` looks equivalent to a
  clamped window and is not: when `-shift` passes the end of the band,
  `rows + shift` is **negative** and Python reads it as "everything but the last N
  rows", so one side stayed non-empty while its partner was empty — a broadcast
  error `(73,150) (0,150)` at large negative shifts only.

## Where the images go

The repository's existing run-artifact scheme (`AGENTS.md` §2), applied by
`artifacts.py` and pinned against `tests/plugins/live_harness.py` by
`tests/vision/test_vision_artifacts.py`:

```
%LOCALAPPDATA%\reaper_reapy_mcp\run_artifacts\<UTC stamp>__vision__<pid>\shots\
```

one directory per **server session**, reused for every shot of that session.
Overrides: `REAPER_MCP_RUN_ARTIFACTS` (root), `REAPER_MCP_EVIDENCE_DIR` (used
verbatim), `REAPER_VISION_DIR` (product-level, used verbatim). Every PNG is
written with a JSON **sidecar** naming the time range it shows, the frame size,
the settle history and the write witness — a PNG cannot say any of that itself,
and the range is read *at capture time*, before any restore.

## Images handed to the model

`max_width` defaults to 1568 px on the long edge: the whole window is 2582 px
wide, so a full-resolution PNG is ~360 kB. `crop` (`"x,y,w,h"` in window pixels,
applied before resizing) is the way to read small text; `max_width=None` sends
full resolution. The file on disk is exactly what was sent (`png_bytes` equals
the length of the image content).

## What a screenshot can and cannot tell you (measured)

A picture of the arrangement is strong evidence for LAYOUT and weak evidence for
CONTENT, and the difference cost real time on 2026-10-18:

* it **can** show: where items are and how long they are, region and marker names,
  the ruler, which tracks exist and in what order, mute/solo/record states, and
  whether the transport is running;
* it **cannot** be trusted about MIDI note content. Measured on
  `futurebass-broken-v1`: the bass item (track 9, one item, 0-63.149 s) holds **59
  notes starting at t = 0** (pitch 30 for 0-3.18 s, 34 for 3.18-6.41 s, 32 for
  6.41-12.80 s). In a scan sheet, and even at full resolution on an 8 s view,
  those LONG notes are a single faint horizontal line, so the first twelve seconds
  read as "empty" while the data says otherwise. Note marks only become obvious in
  the dense passage from ~38 s, where the notes are short.

For an agent this means: take the time range, the item layout and the region names
from the image, and take notes, positions and lengths from the data tools. **A
frame that looks empty is not evidence that an item is empty** - it is evidence
that nothing bright was drawn there. And an empty-looking arrangement has a second
cause that is not about the picture at all: REAPER does not load a project's media
while its window is in the background, so ask `vision_state` for `media` (or pass
`activate=true`) before concluding that the project has no audio.

## Self-check

`python -m reaper_reapy_mcp.vision.cli selftest` (or the REAPER-side shim) prints
a JSON report with: every action id against its live name, the capture-vs-screen
control, that content follows the host, the minimized guard, and the read-only
audit of all eleven operations. Exit code 0 means every check passed.
