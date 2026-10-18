"""The `vision_*` tools: let an agent LOOK at REAPER and move its view.

Registered from `create_server()` with one line and no host work at registration
time (a tool that cannot answer yet must still be listable). Every body resolves
the host lazily and answers with the house envelope: ``{"status": "success" |
"error", ...}`` plus ``hints`` on a refusal - a capture that failed is a typed
answer, never an exception.

WHAT THE AGENT GETS
  * ``vision_state``   - window/view/project state, no image (cheap orientation).
  * ``vision_shot``    - the whole REAPER window as an IMAGE, plus the time range
                         it shows and the read-only witness for that moment.
  * ``vision_view``    - show an exact time range (or shift the current one).
  * ``vision_scroll``  - scroll horizontally/vertically, with the measured shift.
  * ``vision_zoom``    - zoom horizontally (exact factor) or vertically (steps).
  * ``vision_scan``    - tile the arrangement into time slices and return ONE
                         contact sheet plus a manifest of which slice is which.

Image content is returned inline (MCP `ImageContent`) so the model sees the
frame; the same bytes are also written to disk with a JSON sidecar, and the
answer carries both paths. If a client does not render inline images, the paths
are the fallback - the sidecar says which time range the file shows, which the
PNG itself cannot.

HONEST LIMITS, ALL OF THEM MEASURED (see `capture.py` / `view.py`)
  * the capture is REAPER's OWN rendering (`PrintWindow`), so it does not depend
    on which application is active - proven against a screen grab as the control;
  * a minimized window is un-minimized WITHOUT activation, captured, and put
    back the way it was found - the answer says so in `window.*`;
  * the visible SPAN is exact, the POSITION is quantised by the host (2.7e-4 of
    the span); every answer carries the measured errors and the tolerance rule;
  * the view actions are fired only after their NAME was read back from the
    host, so a re-mapped action list cannot be fired into;
  * every audited operation left the project's state counter and undo label
    unchanged, so looking cannot dirty the project - and each answer carries that
    witness (`state_change_delta`) rather than claiming it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, Image

from . import artifacts, capture, imaging, view
from .legend import HOW_TO_READ

__all__ = ["TOOL_NAMES", "setup_vision_tools"]

#: Every tool this layer registers; the offline tests pin this list and each
#: tool's schema, so a parameter the body uses cannot silently fall out of the
#: signature (the server drops an undeclared argument without a word).
TOOL_NAMES: tuple[str, ...] = (
    "vision_state",
    "vision_shot",
    "vision_view",
    "vision_scroll",
    "vision_zoom",
    "vision_scan",
)

#: Default long edge of an image handed to the model. The whole REAPER window is
#: 2582 px wide on a maximized 2560-wide display; the frame is ~360 kB as a
#: full-resolution PNG. 1568 keeps the payload model-sized; `crop` or
#: `max_width=None` is how detail is asked for explicitly.
DEFAULT_MAX_WIDTH = 1568


def _text(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def legend_text() -> str:
    """The legend rendered as plain text.

    `legend.HOW_TO_READ` is the data and this is its only rendering, so the prompt,
    the resource and the CLI cannot drift apart. The tool answers send the DATA
    itself (`how_to_read`), because that is the channel a client always delivers -
    a prompt has to be asked for (it is a `/` command), and a resource has to be
    read by URI.
    """
    lines: list[str] = []
    for section, entries in HOW_TO_READ.items():
        lines.append(section.upper())
        lines.extend(f"- {entry}" for entry in entries)
        lines.append("")
    return "\n".join(lines).strip()


def _answer(payload: dict, image_path: str | None = None,
            image_bytes: bytes | None = None, fmt: str = "png") -> list:
    """The tool result: the JSON answer, plus the image when there is one."""
    blocks: list = []
    if image_bytes is not None:
        blocks.append(Image(data=image_bytes, format=fmt))
    elif image_path:
        blocks.append(Image(path=image_path))
    blocks.append(_text(payload))
    return blocks


def _error(message: str, hints: list[str] | None = None, **extra) -> list:
    payload = {"status": "error", "message": message}
    if hints:
        payload["hints"] = hints
    payload.update(extra)
    return _answer(payload)


def _host():
    """The API module and REAPER's window handle, or a typed refusal."""
    rpr = view.resolve_rpr()
    probe = view.call(rpr.GetMainHwnd)
    if not probe["ok"]:
        raise view.HostUnavailable(
            f"GetMainHwnd failed: {probe['error']} - is REAPER running with the "
            "bridge enabled (Scripts/enable_mcp.py)?"
        )
    hwnd = capture.hwnd_from(probe["value"])
    if not hwnd:
        raise view.HostUnavailable("the host reported an empty window handle")
    return rpr, hwnd


def _window_state(hwnd: int) -> dict:
    return {"hwnd": hex(hwnd), "rect": capture.window_rect(hwnd),
            "minimized": capture.is_minimized(hwnd),
            "maximized": capture.is_maximized(hwnd),
            "foreground": hex(capture.foreground_hwnd())}


def _shoot(hwnd: int, *, label: str, max_width: int | None, crop,
           include_image: bool, settle: bool = True) -> tuple[dict, dict, bytes | None, list | None]:
    """Capture and encode - but do NOT write.

    Saving happens in `_save`, after the caller has added what it knows. This
    split is a fix, not a style choice: the first version wrote the PNG and its
    sidecar inside this function, so the sidecar - whose whole job is to say
    which time range the picture shows - was written BEFORE the range was read,
    and the file could not answer the only question a PNG cannot answer for
    itself (caught by an offline test, 2026-10-18).

    Returns ``(shot, meta, encoded_bytes, refusal)``. `refusal` is not None when
    the frame cannot be trusted (a minimized window whose recovery failed): the
    numbers are reported and NO image is sent, because handing an agent a
    237x39 artefact to reason about is worse than telling it there is no frame.
    """
    shot, info = capture.capture_window(hwnd, settle=settle)
    meta: dict = {"window": {k: info[k] for k in
                             ("minimized_before", "auto_restored", "focus_restored",
                              "minimized_after", "maximized", "visible",
                              "foreground_before", "rect")},
                  "frame_size": list(shot["size"]),
                  "settle": info["settle"],
                  "capture_ms": info["capture_ms_total"],
                  "stats": info["stats"],
                  "looks_like_window": info["looks_like_window"]}
    if not info["looks_like_window"]:
        return shot, meta, None, _error(
            "the captured frame is not a usable window image",
            hints=["the window is minimized and could not be recovered "
                   "(ShowWindow(SW_SHOWNOACTIVATE) failed)",
                   "the frame is reported in `frame_size`; no image was sent so "
                   "that nothing is read from an artefact"],
            **meta,
        )
    if not imaging.HAS_IMAGING:
        return shot, meta, None, _error(
            "the frame was captured but cannot be encoded into an image",
            hints=[f"numpy/Pillow import error: {imaging.IMAGING_ERROR}",
                   "the capture, the statistics and the paths in this answer are valid"],
            **meta,
        )
    data = imaging.encode_image(shot, max_width=max_width, crop=crop)
    meta["image_bytes"] = len(data)
    meta["image_max_width"] = max_width
    return shot, meta, data, None


def _save(shot: dict, meta: dict, data: bytes | None, *, label: str) -> dict:
    """Write the PNG and its sidecar now that the metadata is complete."""
    saved = artifacts.write_shot(shot, meta, label=label, data=data)
    meta.update({"png": saved["png"], "meta_json": saved["meta_json"],
                 "png_bytes": saved["png_bytes"]})
    return meta


def _view_block(rpr) -> dict:
    return {"view": view.view_range(rpr), "h_zoom_px_per_s": view.h_zoom(rpr)}


def _fit_block(rpr, hwnd: int) -> dict:
    """Can the window actually show the range it reports?

    Measured (2026-10-18): `view` is the range REAPER holds for the arrange, and the
    frame shows only as much of it as fits the window MINUS the track panel - a
    panel is user-resizable and can be very wide. A caller asked for 41-45 s at
    393 px/s (1572 px) on a 1721 px window with a ~1000 px panel and received under
    half of the range while the read-back claimed the whole of it, which is exactly
    how a pixel measurement anchored on `view` produces a wrong answer.

    So: `needed_px` (span x zoom) against `window_px`, with a flag when the need
    EXCEEDS the whole window (certainly cut off - the panel makes the real limit
    smaller still). The general answer stays with the caller: read the ruler labels
    inside the frame, because only they say what the picture really shows.
    """
    span = view.view_range(rpr)
    zoom = view.h_zoom(rpr)
    rect = view.call(capture.window_rect, hwnd)
    width = None
    if rect["ok"] and isinstance(rect["value"], (list, tuple)) and len(rect["value"]) == 4:
        width = int(rect["value"][2]) - int(rect["value"][0])
    block: dict = {"window_px": width, "needed_px": None,
                   "ruler_in_frame_is_the_truth": True}
    if span and zoom:
        block["needed_px"] = round((span[1] - span[0]) * zoom, 1)
        block["span_seconds"] = round(span[1] - span[0], 4)
        if width and block["needed_px"] > width:
            block["note"] = ("the range is wider than the whole window at this "
                              "zoom, so the frame certainly shows only part of it - "
                              "zoom out or ask for a shorter range before measuring "
                              "pixels")
        elif width:
            block["note"] = ("whether the whole range fits depends on the track "
                             "panel's width; check the ruler labels drawn in the "
                             "frame")
    return block


def _media_brief(state: dict | None) -> dict | None:
    if state is None:
        return None
    return {"audio_sources": state.get("audio_sources"), "open": state.get("open"),
            "pending": state.get("pending"), "all_open": state.get("all_open")}


def _activate_for_capture(rpr, hwnd: int, timeout: float,
                          restore_focus: bool = True) -> dict:
    """Activate the window and WAIT until the host has opened the media.

    THIS IS ALMOST NEVER NEEDED, and it exists because of a REAPER SETTING the
    user found on 2026-10-18: *Preferences -> Media -> "Set media items offline
    when application is not active"* (`offlineinact` in `reaper.ini`).

      * setting **ON** - a project's audio is closed whenever REAPER is not the
        active application; the mix reported ``length 0.0 / 1 channel / 0 Hz``
        while the file was on disk, its item drew NO waveform, and 1.09 s after the
        window came forward the same call answered ``89.079 s / 2 / 48000``. It is
        unloaded again the moment the focus goes back (0 of 6 sources);
      * setting **OFF** - the media stays loaded in the background and a frame
        taken with REAPER behind another window is **pixel-identical** to one taken
        while it is active (measured: mean |delta| 0.000 over the whole window, 9.06 %
        dark waveform ink in all three moments).

    So this function is a workaround for the ON state only, and callers reach it
    through `activate=True` (default **false**): it brings the window forward -
    which the user explicitly does not want happening on its own - waits for the
    media, and the focus is given back after the frame.

    Failure to activate is NOT a failed capture: the frame is still valid, only
    the media may stay unloaded - which is reported as a warning rather than
    silently passed on.
    """
    before = view.media_sources(rpr)
    settings = view.offline_when_inactive(rpr)
    block: dict = {"requested": True, "restore_focus_requested": bool(restore_focus),
                   "media_settings": {"offline_when_inactive": settings.get("enabled"),
                                      "raw": settings.get("raw"),
                                      "ini": settings.get("ini"),
                                      "setting": settings.get("setting")}}
    if settings.get("note"):
        block["media_settings"]["note"] = settings["note"]
    try:
        block.update(capture.activate_window(hwnd))
    except Exception as exc:  # noqa: BLE001 - the capture continues regardless
        block.update({"ok": False, "method": "FAILED",
                      "error": f"{type(exc).__name__}: {exc}"})
    block["media_before"] = _media_brief(before)
    if block.get("ok"):
        waited = view.wait_for_media(rpr, timeout=timeout)
        after = {"ready": waited["ready"], "open": waited["open"],
                 "audio_sources": waited["audio_sources"],
                 "pending": waited["pending"], "waited_s": waited["waited_s"],
                 "timed_out": waited["timed_out"], "samples": waited["samples"]}
        if waited.get("note"):
            after["note"] = waited["note"]
        block["media_after"] = after
    else:
        block["media_after"] = _media_brief(view.media_sources(rpr))
        block.setdefault("hint", (
            "the window could not be brought to the foreground - if REAPER has the "
            "setting 'Set media items offline when application is not active' ON, "
            "the project's media stays unloaded and its items draw no waveform. "
            "Turning that setting OFF removes the need for activation entirely"))
    block["focus_restored"] = None
    return block
    before = view.media_sources(rpr)
    block: dict = {"requested": True, "restore_focus_requested": bool(restore_focus)}
    try:
        block.update(capture.activate_window(hwnd))
    except Exception as exc:  # noqa: BLE001 - the capture continues regardless
        block.update({"ok": False, "method": "FAILED",
                      "error": f"{type(exc).__name__}: {exc}"})
    block["media_before"] = _media_brief(before)
    if block.get("ok"):
        waited = view.wait_for_media(rpr, timeout=timeout)
        after = {"ready": waited["ready"], "open": waited["open"],
                 "audio_sources": waited["audio_sources"],
                 "pending": waited["pending"], "waited_s": waited["waited_s"],
                 "timed_out": waited["timed_out"], "samples": waited["samples"]}
        if waited.get("note"):
            after["note"] = waited["note"]
        block["media_after"] = after
    else:
        block["media_after"] = _media_brief(view.media_sources(rpr))
        block.setdefault("hint", (
            "the window could not be brought to the foreground, so REAPER may not "
            "have loaded the project's media - `media_after.all_open` says whether "
            "it is loaded, and items without a waveform are then expected"))
    block["focus_restored"] = None
    return block


def _restore_focus(previous: int, hwnd: int, restore_focus: bool) -> dict:
    """Put `previous` back in front, and say what really happened.

    Called AFTER the frame is taken, never before: measured in one run, REAPER
    unloads its media the moment it loses the foreground (0 of 6 sources open and
    the waveform metric back to its flat value immediately after the restore), so
    restoring first would capture the unloaded state.

    Three cases are NOT a restore and are reported as such instead of being
    dressed up: the caller opted out (`restore_focus=False`), there was no window
    to return to (0), or REAPER itself was already the foreground window.
    """
    if not restore_focus:
        return {"ok": True, "method": "skipped (restore_focus=false)",
                "foreground_after": hex(capture.foreground_hwnd())}
    if not previous:
        return {"ok": True, "method": "nothing to restore (no window had the "
                                     "foreground before this call)",
                "foreground_after": hex(capture.foreground_hwnd())}
    if previous == hwnd:
        return {"ok": True, "method": "not needed (REAPER already had it)",
                "foreground_after": hex(capture.foreground_hwnd())}
    try:
        restored = capture.restore_foreground(previous)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"ok": False, "method": "FAILED", "requested": hex(previous),
                "error": f"{type(exc).__name__}: {exc}",
                "foreground_after": hex(capture.foreground_hwnd())}
    restored["requested"] = hex(previous)
    return restored


def _finish_activation(activation: dict, hwnd: int, rpr=None) -> dict:
    """Close an activation block: warn about the outcome, then give the focus back.

    Order is the point - this is called after the capture, so the restore cannot
    unload the media before the frame exists.

    THE RESTORE UNLOADS THE MEDIA AGAIN, and that is not a side effect to hide: it
    is why a project can look empty right after a capture was taken. Measured in
    one run: 6 of 6 sources open, and the waveform drawn, while REAPER was
    foreground; then **0 of 6** and the waveform gone the moment the foreground went
    back. So when `rpr` is given and the focus really moved, the state AFTER the
    restore is read and stated (`media_after_restore` plus a note), because the user
    looking at REAPER a second later sees exactly that and needs to know it is the
    focus and not a broken project.
    """
    warning = _activation_warning(activation)
    if warning:
        activation["warning"] = warning
    previous = int(activation.get("foreground_before") or "0x0", 16)
    restored = _restore_focus(previous, hwnd,
                              bool(activation.get("restore_focus_requested", True)))
    activation["focus_restored"] = restored
    activation["media_after_restore"] = None
    passive = ("skipped", "nothing", "not needed")
    if str(restored.get("method", "")).startswith(passive):
        # REAPER keeps the foreground in every one of these cases, so nothing was
        # unloaded and a second reading would only cost a round trip.
        return activation
    if rpr is None:
        return activation
    try:
        state = view.media_sources(rpr)
    except Exception:  # noqa: BLE001 - this reading is a courtesy, never a failure
        return activation
    activation["media_after_restore"] = _media_brief(state)
    if (state is not None and state["audio_sources"] and not state["all_open"]
            and (activation.get("media_settings") or {}).get("offline_when_inactive")):
        activation["note"] = (
            "REAPER has 'Set media items offline when application is not active' ON, "
            "so it closes the project's media whenever its window is not the active "
            "application (measured: 0 of 6 sources open in the background, 6 of 6 "
            "while active) - and the focus was just given back, so they are closed "
            "again. Items then draw NO waveform even though the files exist. "
            "Turning that setting off (Preferences -> Media) keeps the media loaded "
            "in the background, makes `activate` unnecessary and removes this "
            "whole problem; `restore_focus=false` is the per-call alternative.")
    return activation


def _activation_warning(block: dict) -> str | None:
    """Say plainly when the picture may show media that is not loaded."""
    after = block.get("media_after") or {}
    settings = block.get("media_settings") or {}
    cause = (" - this is REAPER's setting 'Set media items offline when application "
             "is not active' being ON; turning it OFF keeps the media loaded in the "
             "background and removes the need to activate"
             if settings.get("offline_when_inactive") else "")
    if not block.get("ok"):
        return ("activation did not take: with the media-offline setting ON REAPER "
                "loads a project's audio only while its window is in the foreground, "
                "so the frame may show items whose audio is not loaded (no waveform) "
                "even though the files exist" + cause)
    if after.get("ready") is False:
        return (f"only {after.get('open')} of {after.get('audio_sources')} audio "
                f"sources opened within {after.get('waited_s')} s; items without a "
                "waveform may simply not be loaded yet" + cause)
    if after.get("ready") is None:
        return ("the project's items could not be read, so whether the media is "
                "loaded is UNKNOWN - an empty-looking item is not evidence either way")
    return None


def setup_vision_tools(mcp) -> None:
    """Register the vision tools. No host contact happens here."""

    @mcp.tool("vision_state", structured_output=False)
    def vision_state(ctx: Context) -> list:
        """Window, view and project state - the cheap way to orient before a screenshot.

        No image is taken. Answers: the window rect and whether it is minimized
        or maximized, which window currently has focus, the visible time range in
        seconds, the horizontal zoom in pixels per second, tempo, track count,
        project length, the project's write counters (state change count + undo
        label) and the directory this server writes images to.

        It also carries `how_to_read`: THE LEGEND for the arrangement - units and
        how bars map to seconds, what the ruler/region lane/track panel/items/
        grid/cursors are, the recommended orientation workflow, and what a picture
        cannot answer. Read it once and the images make sense; it is returned on
        every call so an agent that has never seen REAPER starts oriented.

        `media` is the cheap answer to "why do the items look empty?", and
        `media_settings` names the reason when there is one: REAPER's setting *Set
        media items offline when application is not active* (`offlineinact`) makes
        it close a project's audio whenever its window is not the active
        application, so the items draw NO waveform even though the files exist
        (measured: 0 of 6 sources open in the background, 6 of 6 while active; with
        the setting OFF the media stays loaded and the frames are pixel-identical).
        `media.pending` names exactly which files are not loaded.

        No activation happens here and none is needed: this call only reads.

        Use it first: `view` tells you which seconds a screenshot would show, and
        `bar_seconds` turns them into bars, so you can decide whether to
        `vision_view` to a range first.
        """
        try:
            rpr, hwnd = _host()
        except view.HostUnavailable as exc:
            return _error(str(exc), hints=["start REAPER and enable the bridge"])
        except Exception as exc:  # noqa: BLE001
            return _error(f"{type(exc).__name__}: {exc}")
        settings = view.offline_when_inactive(rpr)
        before = view.dirty(rpr)
        payload = {
            "status": "success",
            "window": _window_state(hwnd),
            "project": view.project_meta(rpr),
            "media": view.media_sources(rpr),
            "media_settings": settings,
            "dirty": before,
            "imaging": imaging.described_by_env(),
            "how_to_read": HOW_TO_READ,
            "shots_dir": str(artifacts.shots_dir()),
        }
        # The same read-only witness the other tools carry, around this call's own
        # reads: the legend promises `state_change_delta` in every answer, and a
        # promise the state answer does not keep is exactly the kind of drift these
        # tests exist to catch.
        after = view.dirty(rpr)
        payload.update({"dirty_after": after,
                        "state_change_delta": view.dirty_delta(before, after)})
        if settings.get("enabled"):
            payload["note"] = (
                "'Set media items offline when application is not active' is ON, so "
                "a project's audio is only loaded while REAPER's window is the "
                "active application; shots taken with it in the background will show "
                "items without waveforms. Turning the setting off (Preferences -> "
                "Media) removes that, and no activation is then ever needed.")
        return _answer(payload)

    @mcp.tool("vision_shot", structured_output=False)
    def vision_shot(ctx: Context, label: str = "shot",
                    max_width: int | None = DEFAULT_MAX_WIDTH,
                    crop: str | None = None, include_image: bool = True,
                    activate: bool = False,
                    activate_timeout: float = 10.0,
                    restore_focus: bool = True) -> list:
        """Screenshot the whole REAPER window and answer with the IMAGE.

        The frame is REAPER's own rendering, so it does NOT depend on which
        application is active or on top - a covered or minimized REAPER is still
        captured (a minimized window is un-minimized WITHOUT activation for the
        capture and put back afterwards; the answer says so).

        Read `vision_state` first: its `how_to_read` block is the legend for
        everything this picture contains (units, the ruler and region lane, what a
        waveform or MIDI item looks like, what a frame cannot answer).

        Args:
            label: name for the saved file (`<stamp>__<label>.png`).
            max_width: long edge limit of the image handed to you (default 1568).
                The whole window is 2582 px wide, so the default is a legibility
                and payload tradeoff: pass `crop` for detail, or
                `max_width=None` for the full-resolution frame.
            crop: "x,y,w,h" in window pixels, taken BEFORE resizing - this is how
                you read small text (track names, marker labels, the transport).
            include_image: False saves the file and returns only the metadata.
            activate: bring the REAPER window to the FRONT and wait until the
                project's media is OPENED, then take the frame and give the focus
                back. **OFF by default and usually unnecessary** - it exists for one
                case, REAPER's setting *Set media items offline when application is
                not active* (Preferences -> Media): with it ON, the project's audio
                is closed while REAPER is in the background, so items draw no
                waveform (measured: 0 of 6 sources open in the background, 6 of 6
                active, 1.09 s to reload; with the setting OFF the media stays
                loaded and frames taken in the background are pixel-identical).
                Prefer turning the setting off over using this flag: activating
                pulls the user's window to the front, which interrupts them.
            activate_timeout: how long to wait for the media after activating
                (seconds). The wait is driven by the host's own per-source answer,
                not by a sleep, so a project with nothing to load returns at once.
            restore_focus: put the window that had the foreground back in front
                when the capture is done (default true, and only used together with
                `activate`). Activating REAPER would otherwise take the user's
                screen and leave the next thing they type somewhere else; the
                answer's `activation.focus_restored` says whether it worked. Pass
                false when the caller wants REAPER left in front.

        The answer carries the visible time range the frame shows
        (`view`), the frame and image sizes, the read-only witness
        (`state_change_delta`, 0 = the project was not written), the settle
        history (two identical frames are required, so you never receive a
        stale picture of the previous bar) and both file paths.
        """
        try:
            rpr, hwnd = _host()
        except view.HostUnavailable as exc:
            return _error(str(exc), hints=["start REAPER and enable the bridge"])
        try:
            before = view.dirty(rpr)
            activation = (_activate_for_capture(rpr, hwnd, activate_timeout,
                                                restore_focus=restore_focus)
                          if activate else None)
            shot, meta, data, refusal = _shoot(hwnd, label=label, max_width=max_width,
                                              crop=crop, include_image=include_image)
            # The frame exists now, so the user's window may come back: measured,
            # REAPER unloads its media as soon as it loses the foreground.
            if activation is not None:
                meta["activation"] = _finish_activation(activation, hwnd, rpr)
            if refusal is not None:
                return refusal
            after = view.dirty(rpr)
            meta.update(_view_block(rpr))
            meta.update({"status": "success", "dirty_before": before,
                         "dirty_after": after,
                         "state_change_delta": view.dirty_delta(before, after),
                         "project": {"tempo": view.call(rpr.Master_GetTempo)["value"],
                                     "tracks": view.call(rpr.CountTracks, 0)["value"]}})
            _save(shot, meta, data, label=label)
            if not include_image:
                meta["note"] = "include_image=false: the PNG was written, no image was sent"
            return _answer(meta, image_bytes=(data if include_image else None),
                           image_path=(meta["png"] if include_image else None))
        except Exception as exc:  # noqa: BLE001 - a tool must answer, not raise
            return _error(f"{type(exc).__name__}: {exc}")

    @mcp.tool("vision_view", structured_output=False)
    def vision_view(ctx: Context, start: float | None = None, end: float | None = None,
                    shift: float | None = None, restore: bool = True,
                    shot: bool = False, label: str = "view",
                    max_width: int | None = DEFAULT_MAX_WIDTH,
                    pos_tol: float = 0.05) -> list:
        """Make an exact time range visible - then look at it with `shot=true`.

        Args:
            start, end: the range to show, in seconds. Give both.
            shift: move the CURRENT range by this many seconds instead (negative
                moves left). Give it alone, without start/end.
            restore: put the previous range back afterwards (default true, so
                looking at bar 48 does not move the user's window). Pass false
                when you will ask for another navigation call next.
            shot: also return the screenshot of the new range.
            label: name for the saved file when `shot=true`.
            max_width: image long edge limit when `shot=true`.
            pos_tol: allowed position error in seconds (the effective tolerance is
                max(pos_tol, 5e-4 * span), see the answer's `tolerance_rule`).

        The visible SPAN is honoured exactly; the POSITION is quantised by REAPER
        (measured 2.7e-4 of the span, repeatable), which is why the answer reports
        `start_error_s` / `end_error_s` / `tolerance_s` and the range that was
        really set instead of claiming an exact hit.

        Read `vision_state` first for `how_to_read` (units and how bars map to
        seconds); `bar_seconds` from the same answer turns any range into bar
        numbers.
        """
        try:
            rpr, hwnd = _host()
        except view.HostUnavailable as exc:
            return _error(str(exc), hints=["start REAPER and enable the bridge"])
        try:
            entry = view.view_range(rpr)
            if start is None or end is None:
                if shift is None:
                    # The answer is read by a MODEL, not by a shell: it names the
                    # tool and its arguments, never the CLI's flags (the first
                    # version said "--start and --end", which is not a thing this
                    # caller can type).
                    return _error("give start and end together, or shift",
                                  hints=["vision_view(start=0, end=30) shows the "
                                         "first 30 seconds",
                                         "vision_view(shift=30) moves the current "
                                         "range 30 s to the right"])
                if entry is None:
                    return _error("cannot shift: the current view range is unreadable")
                target = [entry[0] + float(shift), entry[1] + float(shift)]
            else:
                target = [float(start), float(end)]
            if target[1] <= target[0]:
                return _error(f"end must be greater than start (got {target[0]}..{target[1]})")
            before = view.dirty(rpr)
            applied = view.set_view(rpr, target[0], target[1], tol=pos_tol)
            payload: dict = {"status": "success" if applied["ok"] else "error",
                             "set": applied, "view_before": entry,
                             "fit": _fit_block(rpr, hwnd),
                             **_view_block(rpr)}
            if not applied["ok"]:
                payload["message"] = ("the range was written but the host did not "
                                      "report it back within tolerance")
                payload["hints"] = [
                    "the span is exact; the position is quantised by REAPER with a "
                    "step that grows with the span (measured 2.7e-4 of the span)",
                    "read `read_back` for the range that is really visible",
                ]
            data = None
            captured: tuple | None = None
            if shot:
                frame, meta, data, refusal = _shoot(hwnd, label=label, max_width=max_width,
                                                    crop=None, include_image=True)
                if refusal is not None:
                    return refusal
                # The range is read HERE, before any restore: the sidecar records
                # what the picture SHOWS, and a value read after the restore
                # would describe a picture nobody took (the first version did
                # exactly that, caught 2026-10-18).
                captured = (frame, meta, view.view_range(rpr))
                payload.update(_view_block(rpr))
            if restore and entry is not None:
                payload["restore"] = view.restore_view(rpr, entry)
            after = view.dirty(rpr)
            payload.update({"dirty_before": before, "dirty_after": after,
                            "state_change_delta": view.dirty_delta(before, after),
                            "view_after": view.view_range(rpr)})
            if captured is not None:
                frame, meta, capture_view = captured
                meta.update({"view": capture_view,
                             "view_after_restore": payload.get("view_after"),
                             "dirty_before": payload["dirty_before"],
                             "dirty_after": payload["dirty_after"],
                             "state_change_delta": payload["state_change_delta"]})
                _save(frame, meta, data, label=label)
                payload["capture"] = meta
            return _answer(payload, image_bytes=data,
                           image_path=(payload.get("capture") or {}).get("png"))
        except Exception as exc:  # noqa: BLE001
            return _error(f"{type(exc).__name__}: {exc}")

    @mcp.tool("vision_scroll", structured_output=False)
    def vision_scroll(ctx: Context, dx: int = 0, dy: int = 0, measure: bool = False,
                      shot: bool = False, restore: bool = True,
                      label: str = "scroll",
                      max_width: int | None = DEFAULT_MAX_WIDTH) -> list:
        """Scroll the arrange view: `dx` horizontally, `dy` vertically.

        Args:
            dx: horizontal units; measured, 1 unit = 16 px (100 units moved the
                view 299.2 s at 5.35 px/s on the check this was built on).
            dy: vertical units; POSITIVE moves the track list DOWN (later tracks
                come into view). Near a limit it clamps, and the answer reports
                the image shift that was really observed instead of predicting one.
            measure: capture before and after and report the measured image shift,
                including how reliable that measurement is (repetitive MIDI parts
                make the correlation multi-peaked - `reliable: false` says so).
            shot: return the screenshot of the result.
            restore: put the view back afterwards (default true).
            label: name for the saved file; max_width: image long edge limit.
        """
        try:
            rpr, hwnd = _host()
        except view.HostUnavailable as exc:
            return _error(str(exc), hints=["start REAPER and enable the bridge"])
        try:
            if not dx and not dy:
                return _error("nothing to scroll: give dx and/or dy",
                              hints=["vision_scroll(dy=200) scrolls the track list down",
                                     "vision_scroll(dx=100) scrolls later in time"])
            entry = view.view_range(rpr)
            before_dirty = view.dirty(rpr)
            pre = None
            if measure:
                pre, _ = capture.capture_window(hwnd, settle=True)
            scrolled = view.scroll(rpr, dx=dx, dy=dy)
            payload: dict = {"status": "success" if scrolled["ok"] else "error",
                             "scroll": scrolled}
            data = None
            captured: tuple | None = None
            if measure or shot:
                frame, meta, data, refusal = _shoot(hwnd, label=label, max_width=max_width,
                                                    crop=None, include_image=True)
                if refusal is not None:
                    return refusal
                captured = (frame, meta, view.view_range(rpr))
                if measure and pre is not None:
                    post, _ = capture.capture_window(hwnd, settle=True)
                    payload["image_shift"] = imaging.vertical_shift(pre, post)
            if restore and entry is not None:
                payload["restore"] = view.restore_view(rpr, entry)
            if not restore:
                payload["note"] = "restore=false: the view stays where this call left it"
            after_dirty = view.dirty(rpr)
            payload.update(_view_block(rpr))
            payload.update({"dirty_before": before_dirty, "dirty_after": after_dirty,
                            "state_change_delta": view.dirty_delta(before_dirty, after_dirty),
                            "view_after": view.view_range(rpr)})
            if captured is not None:
                frame, meta, capture_view = captured
                meta.update({"view": capture_view,
                             "view_after_restore": payload.get("view_after"),
                             "state_change_delta": payload["state_change_delta"]})
                _save(frame, meta, data, label=label)
                payload["capture"] = meta
            return _answer(payload, image_bytes=data,
                           image_path=(payload.get("capture") or {}).get("png"))
        except Exception as exc:  # noqa: BLE001
            return _error(f"{type(exc).__name__}: {exc}")

    @mcp.tool("vision_zoom", structured_output=False)
    def vision_zoom(ctx: Context, h: float | str | None = None, h_steps: int = 1,
                    v: str | None = None, v_steps: int = 1,
                    center: float | None = None, restore: bool = True,
                    shot: bool = False, label: str = "zoom",
                    max_width: int | None = DEFAULT_MAX_WIDTH,
                    pos_tol: float = 0.05) -> list:
        """Zoom the arrange view.

        Args:
            h: "in", "out", or a NUMBER - a FACTOR applied to the visible span
                (>1 zooms out, showing a wider range; 0.5 shows half of it). A
                factor is exact in seconds, so prefer it: it is not a step count.
            h_steps: how many times to fire the horizontal step action when `h` is
                "in"/"out".
            v: "in" or "out" - vertical track zoom. It has no seconds to be exact
                with, so it is a step action.
            v_steps: how many vertical steps.
            center: for a factor, the time to keep in the middle (default: the
                middle of the current range).
            restore: put the view back afterwards (default true). For vertical
                zoom the inverse steps are fired AND verified against the starting
                frame (measured: exactly reversible, pixel-identical).
            shot: return the screenshot after zooming.
            max_width: image long edge limit; pos_tol: allowed position error.

        Horizontal zoom is expressed as the exact visible range; the answer carries
        the range that was really set and the quantisation the host applied.
        """
        try:
            rpr, hwnd = _host()
        except view.HostUnavailable as exc:
            return _error(str(exc), hints=["start REAPER and enable the bridge"])
        try:
            if not h and not v:
                return _error("nothing to zoom: give h and/or v",
                              hints=['vision_zoom(h=0.5) shows half the current span',
                                     'vision_zoom(v="in", v_steps=2) makes the tracks taller'])
            entry = view.view_range(rpr)
            before_dirty = view.dirty(rpr)
            payload: dict = {"status": "success", "view_before": entry}
            pre = None
            if v:
                pre, _ = capture.capture_window(hwnd, settle=True)
            if h is not None:
                # A NUMBER is a factor; the strings are the step actions. The
                # first version declared this parameter as a string only, so the
                # exact factor - the documented way to ask - was refused by the
                # transport before the body ever ran (found by an offline test,
                # 2026-10-18).
                factor: float | None = None
                step_key: str | None = None
                if isinstance(h, str):
                    step_key = {"in": "h_zoom_in", "out": "h_zoom_out"}.get(h.strip().lower())
                    if step_key is None:
                        try:
                            factor = float(h)
                        except ValueError:
                            return _error(f"h must be 'in', 'out' or a number, got {h!r}",
                                          hints=["vision_zoom(h=0.5) shows half the current span",
                                                 "vision_zoom(h='in') fires the step action"])
                else:
                    factor = float(h)
                if step_key is not None:
                    payload["horizontal"] = view.fire_action(rpr, step_key, h_steps)
                elif factor is not None:
                    if factor <= 0:
                        return _error(f"an h factor must be > 0, got {h!r}")
                    if entry is None:
                        return _error("cannot apply a factor: the current view range is unreadable")
                    middle = float(center) if center is not None else (entry[0] + entry[1]) / 2
                    span = (entry[1] - entry[0]) * factor
                    payload["horizontal"] = view.set_view(
                        rpr, middle - span / 2, middle + span / 2, tol=pos_tol)
                    if payload["horizontal"].get("clamped_at_zero"):
                        payload["horizontal"]["hint"] = (
                            "the exact factor could not be centred: a view cannot start "
                            "before 0.0 s. The span is right, the position is not - "
                            "pass center= to choose where it lands, or zoom with the "
                            "\"in\"/\"out\" steps instead.")
            if v:
                key = {"in": "v_zoom_in", "out": "v_zoom_out"}.get(str(v).strip().lower())
                if key is None:
                    return _error(f"v must be 'in' or 'out', got {v!r}")
                payload["vertical"] = view.fire_action(rpr, key, v_steps)
            data = None
            captured: tuple | None = None
            if shot:
                frame, meta, data, refusal = _shoot(hwnd, label=label, max_width=max_width,
                                                    crop=None, include_image=True)
                if refusal is not None:
                    return refusal
                captured = (frame, meta, view.view_range(rpr))
                payload["view_after_zoom"] = view.view_range(rpr)
            if v and restore and pre is not None:
                inverse = "v_zoom_out" if str(v).strip().lower() == "in" else "v_zoom_in"
                payload["vertical_inverse"] = view.fire_action(rpr, inverse, v_steps)
                post, _ = capture.capture_window(hwnd, settle=True)
                payload["vertical_restore"] = {
                    "similarity": imaging.similarity(pre, post),
                    "pixel_identical": (capture.image_stats(pre)["sha256"]
                                        == capture.image_stats(post)["sha256"]),
                }
            if restore and entry is not None:
                payload["restore"] = view.restore_view(rpr, entry)
            if not restore:
                payload["note"] = "restore=false: the view stays where this call left it"
            after_dirty = view.dirty(rpr)
            payload.update(_view_block(rpr))
            payload.update({"dirty_before": before_dirty, "dirty_after": after_dirty,
                            "state_change_delta": view.dirty_delta(before_dirty, after_dirty),
                            "view_after": view.view_range(rpr)})
            if captured is not None:
                frame, meta, capture_view = captured
                meta.update({"view": capture_view,
                             "view_after_restore": payload.get("view_after"),
                             "state_change_delta": payload["state_change_delta"]})
                _save(frame, meta, data, label=label)
                payload["capture"] = meta
            return _answer(payload, image_bytes=data,
                           image_path=(payload.get("capture") or {}).get("png"))
        except Exception as exc:  # noqa: BLE001
            return _error(f"{type(exc).__name__}: {exc}")

    @mcp.tool("vision_scan", structured_output=False)
    def vision_scan(ctx: Context, span_bars: float | None = None,
                    span_seconds: float | None = None, overlap: float = 0.05,
                    start: float | None = None, end: float | None = None,
                    max_tiles: int = 24, columns: int = 2,
                    contact_sheet: bool = True, include_tiles: bool = False,
                    tile_max_width: int = 640, crop: str | None = None,
                    label: str = "scan", restore: bool = True,
                    pos_tol: float = 0.05, activate: bool = False,
                    activate_timeout: float = 10.0,
                    restore_focus: bool = True) -> list:
        """Tile the arrangement into slices and return ONE contact sheet.

        This is the "look at the whole track" call: each slice is captured from a
        precisely set time range, the sheet shows them together with a caption
        per tile, and the manifest says which slice is which file - so a tile you
        want to study can be re-requested with `vision_view` + `vision_shot`.

        Args:
            span_bars: slice length in BARS. The bar length is MEASURED at the
                project's own metre and tempo (`TimeMap_GetTimeSigAtTime`); the
                answer reports which one it used, or says it had to assume 4/4.
            span_seconds: slice length in seconds - used when span_bars is absent.
            overlap: fraction of a slice shared with the next one (0.05 = 5 %).
            start, end: the region to cover; defaults to the whole project.
            max_tiles: hard stop so a long project cannot turn into 200 frames.
            columns: tiles per row in the contact sheet.
            contact_sheet: False skips the sheet (files only).
            include_tiles: also send every tile as its own image (a sheet is
                usually cheaper and enough).
            tile_max_width: width of one tile inside the sheet.
            crop: "x,y,w,h" in window pixels, applied to every tile AND to the
                full-resolution file. This is what makes a scan readable: the
                track panel and mixer are static and identical in every tile, so
                they can be more than half of each picture. Read `vision_state`
                for the window rect, crop away the panel, and the arrangement
                gets all of the tile's pixels.
            label: prefix for the saved files; pos_tol: allowed position error.
            restore: put the previous view range back when the scan is done
                (default true - this is a batch read of the arrangement, not a
                navigation). Pass false to leave the last slice visible.
            activate: same as `vision_shot(activate=...)`, applied ONCE before the
                tiles - a workaround for REAPER's *Set media items offline when
                application is not active* setting, and OFF by default. With that
                setting off, no activation is needed (measured: media stays loaded
                in the background and the frames are pixel-identical).
            activate_timeout: seconds allowed for that wait.
            restore_focus: put the previous foreground window back afterwards
                (default true), so activating REAPER does not take the user's
                screen for the duration of a scan.

        The view is restored afterwards (this is a batch read of the arrangement,
        not a navigation), and the manifest is written beside the images.

        Read `vision_state` first for `how_to_read`, the legend of what a REAPER
        window shows; the captions here carry bars as well as seconds so a tile can
        be named the way the ruler names it.
        """
        try:
            rpr, hwnd = _host()
        except view.HostUnavailable as exc:
            return _error(str(exc), hints=["start REAPER and enable the bridge"])
        try:
            entry = view.view_range(rpr)
            activation = (_activate_for_capture(rpr, hwnd, activate_timeout,
                                                restore_focus=restore_focus)
                          if activate else None)
            meta = view.project_meta(rpr)
            bar = view.bar_seconds(rpr)
            if span_seconds:
                span = float(span_seconds)
                span_source = "seconds"
            elif span_bars:
                if not bar["seconds"]:
                    return _error("cannot convert bars to seconds: no tempo was readable",
                                  hints=["pass span_seconds instead"])
                span = float(span_bars) * bar["seconds"]
                span_source = f"bars ({bar['text']})"
            elif entry is not None:
                span = entry[1] - entry[0]
                span_source = "the current visible range"
            else:
                return _error("give span_bars or span_seconds (the current view "
                              "range is unreadable)")
            if span <= 0:
                return _error(f"the slice length must be positive, got {span}")
            bar_seconds = bar["seconds"] or (4 * 60.0 / (meta.get("tempo") or 120.0))
            overlap = max(0.0, min(0.9, float(overlap)))
            step = span * (1.0 - overlap)
            region_start = float(start) if start is not None else 0.0
            length = meta.get("project_length_s")
            region_end = float(end) if end is not None else (length or region_start + span)
            if region_end <= region_start:
                return _error(f"the region is empty: {region_start}..{region_end}")
            before_dirty = view.dirty(rpr)
            tiles: list[dict] = []
            tile_images: list = []
            tile_captions: list[str] = []
            frame_geometry: dict | None = None
            cursor = region_start
            last_tile_end = region_start
            while cursor < region_end and len(tiles) < int(max_tiles):
                tile_end = min(cursor + span, region_end)
                last_tile_end = tile_end
                applied = view.set_view(rpr, cursor, tile_end, tol=pos_tol)
                shot, info = capture.capture_window(hwnd, settle=True)
                if not info["looks_like_window"]:
                    return _error(
                        "a slice could not be captured as a window image",
                        hints=["the window is minimized and could not be recovered"],
                        frame_size=list(shot["size"]), stats=info["stats"])
                if frame_geometry is None:
                    # `crop` is in WINDOW pixels, so a caller has to know which
                    # frame those pixels belong to: the window geometry changes
                    # between sessions (measured: 1721x1033, then 2582x1550 after
                    # the window was maximized), and a crop computed from an old
                    # screenshot then lands somewhere else entirely.
                    frame_geometry = {"frame_size": list(shot["size"]),
                                      "window_rect": info["rect"],
                                      "crop": crop,
                                      "crop_is_relative_to": "frame pixels, top-left origin"}
                # Downscale ONCE, here: a full-resolution frame is 16 MB, so
                # keeping twenty-four of them for one sheet is 384 MB of RAM.
                small = imaging.tile_image(shot, tile_width=tile_max_width, crop=crop)
                full = imaging.encode_image(shot, crop=crop)
                index = len(tiles)
                bar_from = cursor / bar_seconds + 1
                bar_to = tile_end / bar_seconds + 1
                caption = (f"#{index}  {cursor:.1f}-{tile_end:.1f}s "
                           f"(bars {bar_from:.0f}-{bar_to:.0f})")
                saved = artifacts.write_shot(shot, {"caption": caption,
                                                    "requested": applied["requested"],
                                                    "read_back": applied["read_back"],
                                                    "start_error_s": applied["start_error_s"],
                                                    "slice_seconds": round(span, 4),
                                                    "overlap": overlap},
                                             label=f"{label}_{index:03d}", data=full)
                tiles.append({"index": index, "caption": caption,
                              "start_s": round(cursor, 4), "end_s": round(tile_end, 4),
                              "png": saved["png"], "meta_json": saved["meta_json"],
                              "view_set_ok": applied["ok"],
                              "start_error_s": applied["start_error_s"],
                              "end_error_s": applied["end_error_s"],
                              "read_back": applied["read_back"],
                              "sha256_16": info["stats"]["sha256_16"],
                              "settled": info["settle"]["settled"],
                              "capture_ms": info["capture_ms_total"]})
                tile_images.append(small)
                tile_captions.append(caption)
                cursor += step
            if restore and entry is not None:
                restored = view.restore_view(rpr, entry)
            else:
                restored = None
            after_dirty = view.dirty(rpr)
            # A scan that stopped because of `max_tiles` did NOT reach `end`.
            # Measured live: six 15 s tiles of an 88 s project cover 0-86.25 s, and
            # the answer said nothing, so a caller could believe the whole
            # arrangement had been looked at. The covered range is stated instead.
            # The comparison uses the UNROUNDED tile end: `end_s` is rounded for the
            # record, and rounding 87.928836 to 87.9288 made a scan that reached the
            # last sample report "87.929-87.929 s was NOT covered" (the same number
            # twice - caught by running it, 2026-10-18).
            remaining_s = max(0.0, region_end - last_tile_end)
            truncated = bool(tiles and remaining_s > 1e-6)
            covered_end = last_tile_end
            sheet_data = None
            sheet_meta = None
            if contact_sheet and tile_images:
                sheet_data = imaging.contact_sheet_from_images(
                    tile_images, tile_captions, columns=columns)
                sheet_meta = str(artifacts.write_json(
                    "sheet",
                    {"tiles": tiles, "sheet_bytes": len(sheet_data or b""),
                     "columns": columns, "tile_max_width": tile_max_width},
                    base=Path(tiles[0]["png"])))
            payload: dict = {
                "status": "success",
                "project": {k: v for k, v in meta.items() if k != "view"},
                "slice_seconds": round(span, 4),
                "slice_source": span_source,
                "step_seconds": round(step, 4),
                "overlap": overlap,
                "crop": crop,
                "geometry": frame_geometry,
                "region": [region_start, region_end],
                "covered": [region_start, round(covered_end, 4)],
                "truncated": truncated,
                "truncated_note": (None if not truncated else
                                   f"the scan stopped after {len(tiles)} tiles "
                                   f"(max_tiles), so {covered_end:.3f}-{region_end:.3f} s "
                                   "was NOT covered; raise max_tiles or start the "
                                   "next scan there"),
                "time_signature": bar["signature"],
                "bar_seconds": round(bar_seconds, 4),
                "bar_source": bar["source"],
                "bar_assumed": bar["assumed"],
                "position_quantisation": ("the span is exact; the host quantises the "
                                          "position by ~2.7e-4 of the span, so every "
                                          "tile carries its real read-back range"),
                "tile_count": len(tiles),
                "tiles": tiles,
                "restore": restored,
                "dirty_before": before_dirty, "dirty_after": after_dirty,
                "state_change_delta": view.dirty_delta(before_dirty, after_dirty),
                "view_after": view.view_range(rpr),
                "shots_dir": str(artifacts.shots_dir()),
                "sheet_meta": sheet_meta,
                "sheet_bytes": len(sheet_data or b""),
            }
            if activation is not None:
                payload["activation"] = _finish_activation(activation, hwnd, rpr)
            if include_tiles and tile_images:
                # At most eight tiles are appended: the point of the sheet is to
                # be cheap, and the full-resolution file of any slice is on disk
                # (ask for it with vision_view + vision_shot).
                sent = tile_images[:8]
                payload["tiles_sent"] = len(sent)
                if len(tile_images) > len(sent):
                    payload["tiles_sent_note"] = (
                        f"only the first {len(sent)} of {len(tile_images)} tiles were "
                        "sent; the rest are in the manifest, each with its own file")
                blocks: list = [Image(data=imaging.pil_png_bytes(image), format="png")
                                for image in sent]
                blocks.append(_text(payload))
                return blocks
            return _answer(payload, image_bytes=sheet_data)
        except Exception as exc:  # noqa: BLE001
            return _error(f"{type(exc).__name__}: {exc}")

    # --- the same knowledge as a PROMPT and a RESOURCE --------------------
    #
    # The legend is delivered three ways, on purpose, because they reach the model
    # differently:
    #   * `how_to_read` inside every `vision_state` answer - the only channel that
    #     arrives without the user doing anything, which is what makes the vision
    #     layer self-sufficient on its first call;
    #   * this PROMPT - opt-in, so the full text costs nothing in the common case,
    #     and it is there when a user wants the whole guide in one go;
    #   * this RESOURCE - addressable as `reaper://vision/legend` for any client
    #     that lists or @-mentions resources.
    # Verified against mcp 1.21.2: FastMCP exposes `prompt` / `resource` and lists
    # them back (`list_prompts`, `list_resources`), while `list_tools` is unchanged
    # - so no registered tool can appear or disappear because of these.
    @mcp.prompt("reaper_vision_guide")
    def reaper_vision_guide() -> str:
        """How to read a REAPER arrangement view: the legend behind the vision_* tools.

        Covers units (seconds, bars, beats, pixels), the parts of the window (ruler,
        region lane, track panel, items, grid, cursors, transport), what a drawn
        waveform or MIDI item looks like, the recommended orientation workflow, and
        what a picture cannot tell you.
        """
        return legend_text()

    @mcp.resource("reaper://vision/legend", name="reaper_vision_legend",
                  description="The legend for reading a REAPER arrangement view",
                  mime_type="text/markdown")
    def reaper_vision_legend() -> str:
        """The same legend as a URI-addressable document."""
        return f"# How to read a REAPER arrangement view\n\n{legend_text()}\n"
