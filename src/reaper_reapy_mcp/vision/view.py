"""Move and zoom the arrange view, and read the state back - through reapy.

Every function takes the ReaScript API module as its first argument instead of
importing it, so the whole layer is testable against a fake host (the repo's
offline tests replace `RPR`/`reapy` exactly this way).

MEASURED ON A LIVE HOST (REAPER 7.53, 2026-10-17) - these are the reasons the
code looks the way it does:

* `GetSet_ArrangeView2(proj, isSet, 0, 0, start, end)` returns six items, and
  the last two are the (start, end) seconds that are REALLY visible.
* Setting a range honours the SPAN exactly (span error 0.0 s in every measured
  case) but QUANTISES the position; the step grows with the span - 8.0 ms at a
  31.5 s span, 79.5 ms at a 294 s span, i.e. 2.7e-4 of the span both times. It is
  repeatable for the same request and independent of the zoom level, and a
  second write of the value that came back converges in only 4 of 6 cases - so
  convergence is never claimed. A range REAPER itself produced round-trips
  exactly (1e-12 s), which is why restoring works by writing back a read value.
* `CSurf_OnScroll(dx, 0)` scrolls horizontally at exactly 16 px per unit
  (remeasured here: 100 units moved the view 299.2049 s at 5.3475 px/s). It takes
  TWO arguments - calling it with three raises `takes 2 positional arguments but
  3 were given` inside REAPER (measured), which is easy to mistake for a dead
  bridge.
* `CSurf_OnScroll(0, dy)` scrolls vertically, positive dy moving the track list
  down; near a limit it clamps, so a magnitude is only ever reported as
  MEASURED from the image, never predicted from the unit size.
* The view actions are `View: Zoom in/out horizontal` (1012/1011),
  `View: Zoom in/out vertical` (40111/40112), `View: Scroll view up/down`
  (40138/40139) and `View: Scroll view left/right` (40140/40141). Vertical zoom
  is exactly reversible: three steps in then three out reproduced the starting
  frame pixel for pixel.  * `TimeMap_GetTimeSigAtTime(proj, t, 0, 0, 0.0)` DOES return the out-params
    filled - ``[proj, t, 2, 4, 150.1214]`` - so the metre and the tempo at a
    position can be READ. Measured on a project whose transport displayed exactly
    "2/4" at 150.121 BPM, which is why a bar length no longer has to assume 4/4.* An action id is NEVER fired blindly: its name is read with
  `kbd_getTextFromCmd(id, 0)` and must match, so a re-mapped action list cannot
  be fired into. The mismatch is reported with both names.
* Read-only: every audited operation (set view, scroll, zoom, page actions) left
  `GetProjectStateChangeCount` and `Undo_CanUndo2` unchanged - delta 0, no undo
  change - so moving the view cannot dirty the project.
"""

from __future__ import annotations

import os
import time

__all__ = [
    "HostUnavailable",
    "ACTION_SPEC",
    "resolve_rpr",
    "call",
    "wait_paint",
    "view_range",
    "set_view",
    "restore_view",
    "h_zoom",
    "fire_action",
    "scroll",
    "dirty",
    "dirty_delta",
    "project_meta",
    "project_length",
    "time_signature",
    "bar_seconds",
    "resource_dir",
    "offline_when_inactive",
    "media_sources",
    "wait_for_media",
]

#: key -> (action id, exact action name). The name is part of the contract: it
#: is compared against the host's own copy before the id is fired.
ACTION_SPEC: dict[str, tuple[int, str]] = {
    "h_zoom_in": (1012, "View: Zoom in horizontal"),
    "h_zoom_out": (1011, "View: Zoom out horizontal"),
    "v_zoom_in": (40111, "View: Zoom in vertical"),
    "v_zoom_out": (40112, "View: Zoom out vertical"),
    "v_scroll_up": (40138, "View: Scroll view up"),
    "v_scroll_down": (40139, "View: Scroll view down"),
    "h_scroll_left": (40140, "View: Scroll view left"),
    "h_scroll_right": (40141, "View: Scroll view right"),
}

#: Allowed position error, in seconds, and the relative part that matters once
#: the visible span is wide (the quantisation is 2.7e-4 of the span).
DEFAULT_POS_TOL = 0.05
DEFAULT_REL_TOL = 5e-4


class HostUnavailable(RuntimeError):
    """reapy (or the host behind it) cannot be reached from this process."""


def resolve_rpr():
    """Import the ReaScript API lazily and say so when it cannot be reached.

    The import is done inside a call rather than at module import time: it
    enumerates the host's API through the bridge, so it can only succeed while
    the host is up, and the MCP server must start (and answer with a typed
    error) even when it is not.
    """
    try:
        from reapy import reascript_api as RPR
    except Exception as exc:  # noqa: BLE001 - many failure shapes, one answer
        raise HostUnavailable(
            "reapy.reascript_api is not available in this process "
            f"({type(exc).__name__}: {exc}); start REAPER and the bridge "
            "(Scripts/enable_mcp.py) and retry"
        ) from exc
    return RPR


def call(fn, *args) -> dict:
    """Call a host function and return ``{ok, value, error}`` - never raise.

    The bridge raises many different exception types for a host-side error
    (`DistError`, `TypeError` from the generated signature, connection errors),
    and a tool that answers with a status and the host's own message is more
    useful than one that dies.
    """
    try:
        return {"ok": True, "value": fn(*args), "error": None}
    except Exception as exc:  # noqa: BLE001 - the point of the wrapper
        message = str(exc).strip().splitlines()
        return {"ok": False, "value": None,
                "error": f"{type(exc).__name__}: {message[-1] if message else exc}"}


def wait_paint(seconds: float = 0.3) -> None:
    """Give the host a moment to act on a view change before it is read back."""
    deadline = time.perf_counter() + max(0.0, seconds)
    while time.perf_counter() < deadline:
        time.sleep(0.02)


def _tail(values, count: int = 2):
    if isinstance(values, (list, tuple)) and len(values) >= count:
        try:
            return [float(values[-count]), float(values[-1])] if count == 2 else None
        except (TypeError, ValueError):
            return None
    return None


def view_range(rpr) -> list[float] | None:
    """The (start, end) seconds that are really visible, or None."""
    result = call(rpr.GetSet_ArrangeView2, 0, False, 0, 0, 0.0, 0.0)
    if not result["ok"]:
        return None
    return _tail(result["value"])


def h_zoom(rpr) -> float | None:
    """Horizontal zoom in pixels per second."""
    result = call(rpr.GetHZoomLevel)
    return result["value"] if result["ok"] else None


def set_view(rpr, start: float, end: float, *, tol: float = DEFAULT_POS_TOL,
             rel_tol: float = DEFAULT_REL_TOL, wait: float = 0.3) -> dict:
    """Make `start`..`end` visible and verify it.

    The SPAN is checked tightly (it is honoured exactly). The POSITION is checked
    against ``max(tol, rel_tol * span)``, and the measured errors are always
    reported: the check is relative because REAPER's own quantisation grows with
    the span, and a loosened check must never become a hidden one.
    """
    span = float(end) - float(start)
    effective_tol = max(float(tol), abs(span) * float(rel_tol))
    result = call(rpr.GetSet_ArrangeView2, 0, True, 0, 0, float(start), float(end))
    wait_paint(wait)
    back = view_range(rpr)
    span_error = None if back is None else (back[1] - back[0]) - span
    start_error = None if back is None else back[0] - float(start)
    end_error = None if back is None else back[1] - float(end)
    span_ok = span_error is not None and abs(span_error) <= 1e-6
    position_ok = (start_error is not None and abs(start_error) <= effective_tol
                   and abs(end_error) <= effective_tol)
    # A requested start BEFORE TIME ZERO is not a quantisation error: REAPER cannot
    # show negative time, so it clamps to 0.0 s and slides the window right while
    # still honouring the span. Measured while zooming out 2x on an 88 s project:
    # requested -58.902 s, read back 0.0 s, span exact. Naming the cause is the
    # difference between "the host ignored me" and "this is impossible".
    clamped_at_zero = bool(
        start_error is not None and back is not None
        and abs(start_error) > 1e-9 and float(start) < -1e-9 and back[0] == 0.0)
    payload = {
        "ok": bool(result["ok"] and span_ok and position_ok),
        "call_ok": result["ok"],
        "host_error": result["error"],
        "requested": [float(start), float(end)],
        "read_back": back,
        "span_error_s": span_error,
        "start_error_s": start_error,
        "end_error_s": end_error,
        "tolerance_s": effective_tol,
        "tolerance_rule": f"max({tol:g} s, {rel_tol:g} * span)",
        "error_fraction_of_span": (None if start_error is None or not span
                                   else round(abs(start_error) / abs(span), 8)),
        "clamped_at_zero": clamped_at_zero,
    }
    if clamped_at_zero:
        payload["note"] = (
            f"the requested range starts at {float(start):.3f} s, but a project has no "
            "time before 0.0 s, so the view was clamped to 0.0 s; the SPAN was still "
            "honoured and the position therefore moved")
    return payload


def restore_view(rpr, target, *, wait: float = 0.3) -> dict:
    """Write back a range the host itself produced.

    Such a round-trip was measured EXACT (read-back == requested to 1e-12 s,
    unlike an arbitrary range, which the host quantises). The tolerance is
    therefore floating-point rather than the quantisation allowance `set_view`
    grants, so a drift on restore is reported instead of being absorbed by the
    same rule that hides it everywhere else.
    """
    return set_view(rpr, target[0], target[1], tol=0.0, rel_tol=1e-9, wait=wait)


def fire_action(rpr, key: str, steps: int = 1, *, wait: float = 0.18) -> dict:
    """Fire a view action AFTER reading its name - a mismatched id is refused."""
    if key not in ACTION_SPEC:
        return {"ok": False, "refused": True, "error": f"unknown action key {key!r}",
                "known": sorted(ACTION_SPEC)}
    command, expected = ACTION_SPEC[key]
    probe = call(rpr.kbd_getTextFromCmd, command, 0)
    actual = str(probe["value"]) if probe["ok"] and probe["value"] else None
    if actual != expected:
        return {
            "ok": False, "refused": True, "id": command,
            "expected": expected, "actual": actual, "probe_error": probe["error"],
            "error": (f"action {command} is {actual!r} here, expected {expected!r}; "
                      "refusing to fire an id whose name does not match"),
        }
    out = {"ok": True, "id": command, "name": actual, "fired": 0, "errors": []}
    for _ in range(max(1, int(steps))):
        result = call(rpr.Main_OnCommand, command, 0)
        if not result["ok"]:
            out["ok"] = False
            out["errors"].append(result["error"])
            break
        out["fired"] += 1
        wait_paint(wait)
    wait_paint(wait)
    return out


def scroll(rpr, dx: int = 0, dy: int = 0, *, wait: float = 0.35) -> dict:
    """Scroll by host units: dx horizontally, dy vertically (positive = down).

    Two arguments only - the host's own signature. The horizontal unit is
    16 px (measured), and the result carries the measured seconds moved and the
    observed pixels per unit rather than an assumed one.
    """
    out: dict = {"dx": int(dx), "dy": int(dy), "calls": [], "errors": []}
    before = view_range(rpr)
    out["view_before"] = before
    if dx:
        result = call(rpr.CSurf_OnScroll, int(dx), 0)
        wait_paint(wait)
        out["calls"].append({"axis": "dx", "amount": int(dx), "ok": result["ok"],
                             "host_error": result["error"]})
        if not result["ok"]:
            out["errors"].append(result["error"])
    if dy:
        result = call(rpr.CSurf_OnScroll, 0, int(dy))
        wait_paint(wait)
        out["calls"].append({"axis": "dy", "amount": int(dy), "ok": result["ok"],
                             "host_error": result["error"]})
        if not result["ok"]:
            out["errors"].append(result["error"])
    after = view_range(rpr)
    out["view_after"] = after
    if dx and before and after:
        moved = after[0] - before[0]
        zoom = h_zoom(rpr)
        out["view_shift_s"] = round(moved, 4)
        out["px_per_unit"] = None if not zoom else round(moved / int(dx) * zoom, 3)
    out["ok"] = not out["errors"]
    return out


def dirty(rpr) -> dict:
    """The project's own write counters, used as the read-only witness."""
    return {"state_change_count": call(rpr.GetProjectStateChangeCount, 0)["value"],
            "undo_label": call(rpr.Undo_CanUndo2, 0)["value"]}


def dirty_delta(before: dict, after: dict) -> int | None:
    """Change in the project's state counter; None when either side is unknown.

    ``None`` is deliberate: a counter that could not be read must not be
    reported as "0, nothing was written".
    """
    first, second = before.get("state_change_count"), after.get("state_change_count")
    if first is None or second is None:
        return None
    return second - first


def project_length(rpr) -> float | None:
    result = call(rpr.GetProjectLength, 0)
    return result["value"] if result["ok"] else None


def time_signature(rpr, at_time: float = 0.0) -> dict | None:
    """The metre at `at_time`, read from the host - not assumed.

    Measured 2026-10-18: `TimeMap_GetTimeSigAtTime(proj, t, 0, 0, 0.0)` returns
    ``[proj, t, numerator, denominator, bpm]`` with the out-params genuinely
    filled (the live project answered 2/4 at 150.121 BPM while its transport
    displayed exactly that). Returns None when the call or the shape does not
    allow a reading, so a caller can fall back and SAY it assumed something.
    """
    result = call(rpr.TimeMap_GetTimeSigAtTime, 0, float(at_time), 0, 0, 0.0)
    values = result.get("value")
    if not result["ok"] or not isinstance(values, (list, tuple)) or len(values) < 5:
        return None
    try:
        numerator, denominator, bpm = int(values[2]), int(values[3]), float(values[4])
    except (TypeError, ValueError):
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return {"numerator": numerator, "denominator": denominator, "bpm": bpm,
            "source": "TimeMap_GetTimeSigAtTime"}


def bar_seconds(rpr, at_time: float = 0.0) -> dict:
    """Seconds per bar, measured where possible and honest where not.

    ``bar = numerator * (4 / denominator) * (60 / tempo)`` - one beat is a
    quarter note only in an x/4 metre, which is why the denominator is carried
    through instead of being dropped.
    """
    signature = time_signature(rpr, at_time)
    tempo = (signature or {}).get("bpm") or call(rpr.Master_GetTempo)["value"]
    if not tempo:
        return {"seconds": None, "tempo": None, "signature": signature,
                "source": "unknown", "assumed": "no tempo could be read"}
    if signature:
        seconds = (signature["numerator"] * (4.0 / signature["denominator"])
                   * 60.0 / tempo)
        return {"seconds": seconds, "tempo": tempo, "signature": signature,
                "source": "measurement", "assumed": None,
                "text": f"{signature['numerator']}/{signature['denominator']} "
                        f"at {tempo:g} BPM"}
    return {"seconds": 4 * 60.0 / tempo, "tempo": tempo, "signature": None,
            "source": "assumption", "assumed": "4/4 (the metre could not be read)",
            "text": f"4/4 assumed at {tempo:g} BPM"}


def project_meta(rpr) -> dict:
    """Tempo, track count, project length, METRE, bars, visible range and zoom.

    The metre is read (`TimeMap_GetTimeSigAtTime`); `time_signature` is None when
    the host will not answer, and every caller that derives a bar length from it
    has to say which of the two it used (`bar_seconds` does exactly that).

    `bar_seconds` / `beat_seconds` are included because orientation in an
    arrangement means converting between what the ruler shows and the seconds
    every other field is in - an agent should not have to know that a bar is
    `numerator * (4 / denominator) * 60 / tempo`, which is only a quarter note when
    the metre is x/4 (this project is 2/4, so a bar is 0.799 s, not 1.6 s).
    """
    bar = bar_seconds(rpr)
    beat = (bar["seconds"] / bar["signature"]["numerator"]
            if bar.get("seconds") and bar.get("signature") else None)
    return {"tempo": call(rpr.Master_GetTempo)["value"],
            "tracks": call(rpr.CountTracks, 0)["value"],
            "project_length_s": project_length(rpr),
            "time_signature": time_signature(rpr),
            "bar_seconds": bar["seconds"],
            "beat_seconds": beat,
            "bar_source": bar["source"],
            "view": view_range(rpr),
            "h_zoom_px_per_s": h_zoom(rpr)}


def resource_dir(rpr) -> str | None:
    """REAPER's resource directory (where `reaper.ini` lives), read from the host.

    Measured: `GetResourcePath()` takes **no arguments** through reapy (passing a
    buffer raises `takes 0 positional arguments but 1 was given`) and answers
    ``'C:\\REAPER'`` on this install.
    """
    result = call(rpr.GetResourcePath)
    value = result.get("value")
    if result["ok"] and isinstance(value, str) and value:
        return value
    return None


def offline_when_inactive(rpr, *, ini_path: str | None = None) -> dict:
    """Read *Preferences -> Media -> "Set media items offline when application is not active"*.

    THIS IS THE SETTING THAT MADE EVERYTHING LOOK BROKEN, and the user found it
    (2026-10-18) after the behaviour had been measured from the other side:

      * with it **ON** (`offlineinact=1`), a project's media is closed whenever
        REAPER is not the active application - measured: 0 of 6 sources open and
        `GetMediaSourceLength = 0.0` while the window was in the background, and the
        items drew NO waveform; activating the window reopened them after ~3.5 s;
      * with it **OFF** (`offlineinact=0`), the media stays loaded in the
        background - measured: 6 of 6 sources open, and frames taken while REAPER
        was in the background are **pixel-identical** to frames taken while it was
        active (mean |delta| 0.000 over the whole window).

    So this is both the explanation and the FIX: with the setting off, no
    activation and no wait are needed at all.

    `ini_path` is for tests; otherwise the file is resolved through the host's own
    resource path. Returns ``{enabled, raw, key, setting, ini, source}`` where
    ``enabled`` is None when it cannot be read - "unknown" is never reported as
    "off", because off is the claim that makes an empty item surprising.
    """
    info: dict = {
        "key": "offlineinact",
        "setting": ("Preferences -> Media -> Set media items offline when "
                    "application is not active"),
        "ini": None, "enabled": None, "raw": None, "source": None,
    }
    if ini_path is None:
        directory = resource_dir(rpr)
        if not directory:
            info["note"] = ("REAPER's resource path could not be read, so the "
                            "setting is UNKNOWN (not confirmed off)")
            return info
        ini_path = os.path.join(directory, "reaper.ini")
    info["ini"] = ini_path
    try:
        with open(ini_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip().startswith("offlineinact="):
                    raw = line.split("=", 1)[1].strip()
                    info["raw"] = raw
                    info["enabled"] = raw not in ("0", "")
                    info["source"] = "reaper.ini"
                    return info
    except OSError as exc:
        info["note"] = (f"reaper.ini could not be read ({type(exc).__name__}: "
                        f"{exc}), so the setting is UNKNOWN")
        return info
    info["note"] = ("`offlineinact` is not present in reaper.ini, so the setting is "
                    "UNKNOWN (REAPER writes a key when it differs from the default)")
    return info


def _buffer_text(result) -> str | None:
    """The text out of a reapy buffer call.

    reapy hands buffer-argument functions back as the RAW C tuple and the text
    sits in slot 1 for `GetMediaSourceFileName` / `GetMediaSourceType` - measured
    (2026-10-18): ``GetMediaSourceFileName(src, "", 512)`` answered
    ``['(PCM_source*)0xA5D16C0', 'C:\\\\...\\\\song.wav', 512]``, while
    ``EnumProjects`` puts it in slot 2. Reading the wrong slot is how a path came
    back as a POINTER's repr, so this reads 1 and falls back to 2.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        for index in (1, 2):
            if len(result) > index and isinstance(result[index], str):
                return result[index]
    return None


def media_sources(rpr, *, limit: int = 64) -> dict | None:
    """How many of the project's audio sources REAPER has actually OPENED.

    Measured (2026-10-18), and the reason this function exists at all: while the
    REAPER window is in the BACKGROUND, a project's media is not loaded - the mix
    reported ``GetMediaSourceLength = 0.0``, 1 channel and 0 Hz although the file
    was on disk and named by the project, and its item consequently drew NO
    WAVEFORM. 1.09 s after the window was brought to the foreground the same call
    reported 89.079 s / 2 / 48000. A screenshot of an empty-looking project is
    therefore not necessarily a capture fault.

    A source counts as OPEN when it reports a positive length. MIDI takes are
    counted separately and never hold the wait up: they have no file to stream.
    Scans at most `limit` items so a large project cannot turn one state question
    into a thousand round trips, and reports how many it looked at.
    """
    tracks = call(rpr.CountTracks, 0)
    if not tracks["ok"] or not isinstance(tracks["value"], int):
        return None
    audio = opened = midi = scanned = 0
    pending: list[str] = []
    for index in range(int(tracks["value"])):
        track = rpr.GetTrack(0, index)
        count = call(rpr.CountTrackMediaItems, track)
        if not count["ok"] or not isinstance(count["value"], int):
            continue
        for item_index in range(int(count["value"])):
            if scanned >= limit:
                break
            item = rpr.GetTrackMediaItem(track, item_index)
            take = rpr.GetMediaItemTake(item, 0)
            source = rpr.GetMediaItemTake_Source(take)
            if not source:
                continue
            scanned += 1
            kind = _buffer_text(call(rpr.GetMediaSourceType, source, "", 512)["value"])
            name = _buffer_text(call(rpr.GetMediaSourceFileName, source, "", 512)["value"])
            if kind and kind.upper() == "MIDI":
                midi += 1
                continue
            length = call(rpr.GetMediaSourceLength, source, False)["value"]
            if isinstance(length, (list, tuple)) and length:
                length = length[0]
            try:
                seconds = float(length)
            except (TypeError, ValueError):
                continue
            audio += 1
            if seconds > 0:
                opened += 1
            elif len(pending) < 8:
                pending.append(name or f"item {item_index}")
        if scanned >= limit:
            break
    return {"scanned": scanned, "audio_sources": audio, "open": opened, "midi": midi,
            "pending": pending, "all_open": audio == opened,
            "source": "GetMediaSourceLength"}


def wait_for_media(rpr, *, timeout: float = 10.0, interval: float = 0.25) -> dict:
    """Wait until REAPER has OPENED the project's audio sources, or give up.

    This is the second half of the pair that starts with
    `capture.activate_window`: measured, the media is opened only once the window
    is in the foreground, and it took 1.09 s on a six-source project. The wait is
    driven by the host's own answer (`media_sources`), never by a fixed sleep - a
    sleep long enough for this project would be wrong on the next one, and a
    fixed timeout would report "ready" without having looked.

    Returns ``{ready, waited_s, timed_out, audio_sources, open, pending, samples}``;
    ``ready`` is None when the project could not be read at all, which is
    different from "read, and nothing is open".
    """
    started = time.perf_counter()
    samples: list[dict] = []
    state: dict | None = None
    while True:
        state = media_sources(rpr)
        elapsed = time.perf_counter() - started
        if state is None:
            return {"ready": None, "waited_s": round(elapsed, 3), "timed_out": False,
                    "audio_sources": None, "open": None, "pending": [],
                    "samples": samples,
                    "note": "the project's items could not be read, so whether the "
                            "media is loaded is UNKNOWN - not a claim that it is"}
        samples.append({"t": round(elapsed, 3), "open": state["open"],
                        "audio_sources": state["audio_sources"]})
        if state["all_open"] or elapsed >= timeout:
            break
        # Never sleep past the promised timeout: with a 50 ms budget and a 250 ms
        # interval the first version waited 250 ms, i.e. five times what the
        # caller asked for (the answer said "within 0.25 s" for a 0.05 s request).
        time.sleep(min(interval, max(0.0, timeout - elapsed)))
    assert state is not None
    ready = bool(state["all_open"])
    result = {"ready": ready, "waited_s": round(time.perf_counter() - started, 3),
              "timed_out": not ready, "audio_sources": state["audio_sources"],
              "open": state["open"], "pending": state["pending"],
              "samples": samples[:12]}
    if not ready:
        result["note"] = (
            f"{state['open']} of {state['audio_sources']} audio sources are open "
            f"after {result['waited_s']} s; REAPER opens a project's media when its "
            "window is in the foreground, so the picture may show items that are "
            "not loaded yet")
    return result

