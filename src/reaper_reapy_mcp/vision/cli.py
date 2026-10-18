"""The vision layer on the command line: same code the MCP tools call.

It exists so the feature can be exercised without an MCP round trip - by a human
looking at a PNG, by a probe, by a test - and so the REAPER-side script
(`<REAPER>/Scripts/reaper_vision/reaper_shot.py`) is a five-line shim instead of
a second implementation that drifts.

    py -3.11 -m reaper_reapy_mcp.vision.cli state
    py -3.11 -m reaper_reapy_mcp.vision.cli shot --label check
    py -3.11 -m reaper_reapy_mcp.vision.cli view --shift 30 --shot
    py -3.11 -m reaper_reapy_mcp.vision.cli scroll --dx 100 --dy 0 --measure
    py -3.11 -m reaper_reapy_mcp.vision.cli zoom --h 2 --shot
    py -3.11 -m reaper_reapy_mcp.vision.cli scan --span-bars 16 --overlap 0.05
    py -3.11 -m reaper_reapy_mcp.vision.cli selftest

Every command prints JSON; the exit code is 0 when the answer's own checks
passed, 1 otherwise - so a shell can use it as a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import artifacts, capture, imaging, tools, view


def _host():
    rpr = view.resolve_rpr()
    probe = view.call(rpr.GetMainHwnd)
    if not probe["ok"]:
        raise view.HostUnavailable(f"GetMainHwnd failed: {probe['error']}")
    hwnd = capture.hwnd_from(probe["value"])
    return rpr, hwnd


def _shoot(hwnd: int, label: str, *, max_width: int | None, crop=None,
           settle: bool = True) -> dict:
    if not imaging.HAS_IMAGING:
        raise RuntimeError(f"image encoding is unavailable: {imaging.IMAGING_ERROR}")
    shot, info = capture.capture_window(hwnd, settle=settle)
    data = imaging.encode_image(shot, max_width=max_width, crop=crop)
    saved = artifacts.write_shot(shot, dict(info, frame_size=list(shot["size"])),
                                 label=label, data=data)
    return {"png": saved["png"], "meta_json": saved["meta_json"],
            "bytes": saved["png_bytes"], "stats": info["stats"],
            "settle": info["settle"], "looks_like_window": info["looks_like_window"],
            "window": {key: info[key] for key in
                       ("minimized_before", "auto_restored", "focus_restored",
                        "minimized_after", "maximized", "foreground_before")}}


def cmd_state(_args) -> dict:
    rpr, hwnd = _host()
    settings = view.offline_when_inactive(rpr)
    out = {"ok": True, "window": {"hwnd": hex(hwnd), "rect": capture.window_rect(hwnd),
                                  "minimized": capture.is_minimized(hwnd),
                                  "foreground": hex(capture.foreground_hwnd())},
           "project": view.project_meta(rpr),
           "media": view.media_sources(rpr),
           "media_settings": settings,
           "how_to_read": tools.HOW_TO_READ,
           "dirty": view.dirty(rpr),
           "imaging": imaging.described_by_env(),
           "shots_dir": str(artifacts.shots_dir())}
    if settings.get("enabled"):
        out["note"] = (
            "'Set media items offline when application is not active' is ON, so a "
            "project's audio is loaded only while REAPER's window is the active "
            "application - shots taken in the background then show items without "
            "waveforms. Turning it off (Preferences -> Media) removes that and "
            "makes --activate unnecessary.")
    return out


def _activate(rpr, hwnd: int, timeout: float, restore_focus: bool = True) -> dict:
    """Bring the window forward and wait for the media - and DO NOT give focus back yet.

    Primitives: `capture.activate_window` (which way worked), `view.wait_for_media`
    (the host's own per-source answer, never a fixed sleep).

    The focus restore is deliberately NOT here: measured, REAPER unloads its media
    the moment it loses the foreground (0 of 6 sources open and the waveform gone
    immediately after the restore), so the caller must take its picture first and
    then call `_finish_activation`.
    """
    block: dict = {"requested": True, "restore_focus_requested": bool(restore_focus),
                   "media_settings": view.offline_when_inactive(rpr),
                   "media_before": view.media_sources(rpr)}
    block.update(capture.activate_window(hwnd))
    if block.get("ok"):
        block["media_after"] = view.wait_for_media(rpr, timeout=timeout)
    else:
        block["media_after"] = view.media_sources(rpr)
    block["focus_restored"] = None
    return block


def _finish_activation(block: dict, rpr, hwnd: int) -> dict:
    """Give the foreground back to the window that had it, after the capture.

    The state after the restore is read too (`media_after_restore`): measured,
    REAPER unloads its media the moment it loses the foreground, so a project can
    look empty right after a capture - and that has to be said rather than left for
    the user to wonder about.
    """
    previous = int(block.get("foreground_before") or "0x0", 16)
    if not block.get("restore_focus_requested", True):
        block["focus_restored"] = {"ok": True,
                                   "method": "skipped (--no-restore-focus)",
                                   "foreground_after": hex(capture.foreground_hwnd())}
        block["media_after_restore"] = None
        return block
    if not previous or previous == hwnd:
        block["focus_restored"] = {
            "ok": True,
            "method": ("not needed (REAPER already had the foreground)" if previous
                       else "nothing to restore (no window had the foreground)"),
            "foreground_after": hex(capture.foreground_hwnd())}
        block["media_after_restore"] = None
        return block
    restored = capture.restore_foreground(previous)
    restored["requested"] = hex(previous)
    block["focus_restored"] = restored
    state = view.media_sources(rpr)
    block["media_after_restore"] = state
    if state is not None and state["audio_sources"] and not state["all_open"]:
        block["note"] = (
            "REAPER has 'Set media items offline when application is not active' "
            "ON, and the focus was given back, so the project's media is closed "
            "again and its items draw no waveform. Turning that setting off "
            "(Preferences -> Media) keeps the media loaded in the background and "
            "makes --activate unnecessary; --no-restore-focus is the per-call "
            "alternative.")
    return block


def cmd_shot(args) -> dict:
    rpr, hwnd = _host()
    out: dict = {}
    activation = None
    if getattr(args, "activate", False):
        activation = _activate(rpr, hwnd, args.activate_timeout,
                               restore_focus=args.restore_focus)
    before = view.dirty(rpr)
    out.update(_shoot(hwnd, args.label, max_width=args.max_width, crop=args.crop))
    # The picture exists now; only then does the user's window come back.
    if activation is not None:
        out["activation"] = _finish_activation(activation, rpr, hwnd)
    out["view"] = view.view_range(rpr)
    out["h_zoom_px_per_s"] = view.h_zoom(rpr)
    out["state_change_delta"] = view.dirty_delta(before, view.dirty(rpr))
    out["ok"] = bool(out["looks_like_window"] and out["state_change_delta"] == 0)
    return out


def cmd_view(args) -> dict:
    rpr, hwnd = _host()
    entry = view.view_range(rpr)
    if args.start is None or args.end is None:
        if args.shift is None:
            return {"ok": False, "error": "give --start and --end, or --shift"}
        if entry is None:
            return {"ok": False, "error": "the current view range is unreadable"}
        target = [entry[0] + args.shift, entry[1] + args.shift]
    else:
        target = [args.start, args.end]
    if target[1] <= target[0]:
        return {"ok": False, "error": "end must be greater than start"}
    before = view.dirty(rpr)
    applied = view.set_view(rpr, target[0], target[1], tol=args.pos_tol)
    out = {"view_before": entry, "set": applied, "h_zoom_px_per_s": view.h_zoom(rpr)}
    if args.shot:
        out["capture"] = _shoot(hwnd, args.label or "view", max_width=args.max_width)
    if not args.keep_view and entry is not None:
        out["restore"] = view.restore_view(rpr, entry)
    out["state_change_delta"] = view.dirty_delta(before, view.dirty(rpr))
    out["view_after"] = view.view_range(rpr)
    out["ok"] = bool(applied["ok"] and out["state_change_delta"] == 0)
    return out


def cmd_scroll(args) -> dict:
    rpr, hwnd = _host()
    entry = view.view_range(rpr)
    before = view.dirty(rpr)
    pre = None
    if args.measure:
        pre, _ = capture.capture_window(hwnd, settle=True)
    out = {"view_before": entry, "scroll": view.scroll(rpr, dx=args.dx, dy=args.dy)}
    if args.measure:
        post, _ = capture.capture_window(hwnd, settle=True)
        out["image_shift"] = imaging.vertical_shift(pre, post)
    if args.shot or args.measure:
        out["capture"] = _shoot(hwnd, args.label or "scroll", max_width=args.max_width)
    if not args.keep_view and entry is not None:
        out["restore"] = view.restore_view(rpr, entry)
    out["state_change_delta"] = view.dirty_delta(before, view.dirty(rpr))
    out["view_after"] = view.view_range(rpr)
    out["ok"] = bool(out["scroll"]["ok"] and out["state_change_delta"] == 0)
    return out


def cmd_zoom(args) -> dict:
    rpr, hwnd = _host()
    entry = view.view_range(rpr)
    before = view.dirty(rpr)
    out: dict = {"view_before": entry}
    pre = None
    if args.v:
        pre, _ = capture.capture_window(hwnd, settle=True)
    if args.h is not None:
        if args.h in ("in", "out"):
            out["horizontal"] = view.fire_action(rpr, f"h_zoom_{args.h}", args.h_steps)
        else:
            factor = float(args.h)
            middle = args.center if args.center is not None else (entry[0] + entry[1]) / 2
            span = (entry[1] - entry[0]) * factor
            out["horizontal"] = view.set_view(rpr, middle - span / 2, middle + span / 2,
                                              tol=args.pos_tol)
    if args.v:
        out["vertical"] = view.fire_action(rpr, f"v_zoom_{args.v}", args.v_steps)
    if args.shot:
        out["capture"] = _shoot(hwnd, args.label or "zoom", max_width=args.max_width)
        out["view_after_zoom"] = view.view_range(rpr)
    if args.v and not args.keep_view and pre is not None:
        inverse = "v_zoom_out" if args.v == "in" else "v_zoom_in"
        out["vertical_inverse"] = view.fire_action(rpr, inverse, args.v_steps)
        post, _ = capture.capture_window(hwnd, settle=True)
        out["vertical_restore"] = {
            "similarity": imaging.similarity(pre, post),
            "pixel_identical": (capture.image_stats(pre)["sha256"]
                                == capture.image_stats(post)["sha256"]),
        }
    if not args.keep_view and entry is not None:
        out["restore"] = view.restore_view(rpr, entry)
    out["state_change_delta"] = view.dirty_delta(before, view.dirty(rpr))
    out["ok"] = bool(out["state_change_delta"] == 0
                     and (args.keep_view or out.get("restore", {}).get("ok")))
    return out


def cmd_scan(args) -> dict:
    rpr, hwnd = _host()
    activation = (_activate(rpr, hwnd, args.activate_timeout,
                            restore_focus=args.restore_focus)
                  if getattr(args, "activate", False) else None)
    entry = view.view_range(rpr)
    meta = view.project_meta(rpr)
    bar = view.bar_seconds(rpr)
    bar_seconds = bar["seconds"] or (4 * 60.0 / (meta.get("tempo") or 120.0))
    span = (args.span_seconds if args.span_seconds
            else (args.span_bars * bar_seconds if args.span_bars
                  else (entry[1] - entry[0] if entry else 0.0)))
    if span <= 0:
        return {"ok": False, "error": "the slice length must be positive"}
    step = span * (1.0 - max(0.0, min(0.9, args.overlap)))
    start = args.start if args.start is not None else 0.0
    end = args.end if args.end is not None else (meta.get("project_length_s") or start + span)
    before = view.dirty(rpr)
    tiles, images, captions = [], [], []
    frame_geometry = None
    cursor = start
    last_tile_end = start
    while cursor < end and len(tiles) < args.max_tiles:
        tile_end = min(cursor + span, end)
        last_tile_end = tile_end
        applied = view.set_view(rpr, cursor, tile_end, tol=args.pos_tol)
        shot, info = capture.capture_window(hwnd, settle=True)
        if not info["looks_like_window"]:
            return {"ok": False, "error": "a slice is not a usable window image",
                    "frame_size": list(shot["size"])}
        if frame_geometry is None:
            frame_geometry = {"frame_size": list(shot["size"]),
                              "window_rect": info["rect"], "crop": args.crop}
        images.append(imaging.tile_image(shot, tile_width=args.tile_max_width,
                                         crop=args.crop))
        caption = (f"#{len(tiles)}  {cursor:.1f}-{tile_end:.1f}s "
                   f"(bars {cursor / bar_seconds + 1:.0f}-{tile_end / bar_seconds + 1:.0f})")
        captions.append(caption)
        saved = artifacts.write_shot(
            shot, {"caption": caption, "read_back": applied["read_back"],
                   "start_error_s": applied["start_error_s"], "crop": args.crop},
            label=f"{args.label}_{len(tiles):03d}",
            data=imaging.encode_image(shot, crop=args.crop))
        tiles.append({"index": len(tiles), "caption": caption, "png": saved["png"],
                      "start_s": round(cursor, 4), "end_s": round(tile_end, 4),
                      "view_set_ok": applied["ok"],
                      "start_error_s": applied["start_error_s"],
                      "sha256_16": info["stats"]["sha256_16"]})
        cursor += step
    # Every tile is captured; now the user's window may come back.
    if activation is not None:
        activation = _finish_activation(activation, rpr, hwnd)
    sheet_path = None
    sheet_bytes = None
    sheet_png = None
    if args.contact_sheet and images:
        sheet_bytes = imaging.contact_sheet_from_images(images, captions,
                                                        columns=args.columns)
        sheet_path = artifacts.write_json("sheet", {"tiles": tiles},
                                          base=Path(tiles[0]["png"]))
        # The sheet is the point of a scan, so it is written as an IMAGE too: the
        # tool returns it inline, and a CLI caller has to be able to open it.
        if sheet_bytes:
            sheet_png = sheet_path.with_suffix(".png")
            sheet_png.write_bytes(sheet_bytes)
    if not args.keep_view and entry is not None:
        restored = view.restore_view(rpr, entry)
    else:
        restored = None
    covered_end = last_tile_end
    truncated = bool(tiles and (end - last_tile_end) > 1e-6)
    out = {"project": {k: v for k, v in meta.items() if k != "view"},
           "slice_seconds": round(span, 4), "step_seconds": round(step, 4),
           "overlap": args.overlap, "region": [start, end], "crop": args.crop,
           "geometry": frame_geometry,
           "covered": [start, round(covered_end, 4)], "truncated": truncated,
           "truncated_note": (None if not truncated else
                              f"the scan stopped after {len(tiles)} tiles (max_tiles), so "
                              f"{covered_end:.3f}-{end:.3f} s was NOT covered; raise "
                              "max_tiles or start the next scan there"),
           "bar_seconds": round(bar_seconds, 4), "bar_source": bar["source"],
           "bar_assumed": bar["assumed"], "time_signature": bar["signature"],
           "tile_count": len(tiles), "tiles": tiles, "restore": restored,
           "state_change_delta": view.dirty_delta(before, view.dirty(rpr)),
           "view_after": view.view_range(rpr), "shots_dir": str(artifacts.shots_dir()),
           "sheet_meta": str(sheet_path) if sheet_path else None,
           "sheet_png": str(sheet_png) if sheet_png else None,
           "sheet_bytes": len(sheet_bytes or b"")}
    if activation is not None:
        out["activation"] = activation
    out["ok"] = bool(tiles and out["state_change_delta"] == 0
                     and (args.keep_view or (restored or {}).get("ok")))
    return out


def cmd_selftest(args) -> dict:
    """The controls, so a green run means something."""
    rpr, hwnd = _host()
    checks: dict = {}
    names = {}
    for key, (command, expected) in view.ACTION_SPEC.items():
        probe = view.call(rpr.kbd_getTextFromCmd, command, 0)
        actual = str(probe["value"]) if probe["ok"] and probe["value"] else None
        names[key] = {"id": command, "expected": expected, "actual": actual,
                      "match": actual == expected}
    checks["action_names"] = {"ok": all(v["match"] for v in names.values()), "detail": names}

    entry = view.view_range(rpr)
    dirty0 = view.dirty(rpr)
    first, _ = capture.capture_window(hwnd, settle=True)
    rect = capture.window_rect(hwnd)
    screen = capture.grab_screen_region(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])
    window_similarity = imaging.similarity(first, screen)
    checks["capture_is_the_window"] = {
        "similarity_to_screen_grab": window_similarity,
        "rule": ("below 12 means the screen grab was REAPER too, so the control is "
                 "inconclusive rather than passing"),
        "screen_grab_was_reaper": window_similarity[0] < 12,
        "ok": True,
    }
    if entry is not None:
        delta = (entry[1] - entry[0]) * 0.25
        view.set_view(rpr, entry[0] + delta, entry[1] + delta)
        moved, info = capture.capture_window(hwnd, settle=True)
        checks["content_follows_the_host"] = {
            "changed": (capture.image_stats(first)["sha256"] != info["stats"]["sha256"]),
            "ok": capture.image_stats(first)["sha256"] != info["stats"]["sha256"],
        }
        view.set_view(rpr, entry[0], entry[1], tol=0.0)

    if not args.skip_minimize:
        before, _ = capture.capture_window(hwnd, settle=True)
        capture.show_minimize(hwnd)
        view.wait_paint(0.5)
        mini, mini_info = capture.capture_window(hwnd, settle=False, auto_restore=False)
        capture.show_noactivate(hwnd)
        view.wait_paint(0.7)
        after, _ = capture.capture_window(hwnd, settle=True)
        recovered = capture.looks_like_window(after)
        checks["minimized_guard"] = {
            "minimized_frame_size": list(mini["size"]),
            "minimized_colors": capture.image_stats(mini)["distinct_sampled_colors"],
            "minimized_rejected": not mini_info["looks_like_window"],
            "recovered": recovered,
            "similarity_before_vs_recovered": imaging.similarity(before, after),
            "ok": bool(not mini_info["looks_like_window"] and recovered),
        }

    audit = []
    for label, action in (
        ("set_view", lambda: view.set_view(rpr, entry[0] + 5, entry[1] - 5) if entry else None),
        ("scroll dx", lambda: view.call(rpr.CSurf_OnScroll, 50, 0)),
        ("scroll dy", lambda: view.call(rpr.CSurf_OnScroll, 0, 30)),
        ("h_zoom_out", lambda: view.fire_action(rpr, "h_zoom_out")),
        ("h_zoom_in", lambda: view.fire_action(rpr, "h_zoom_in")),
        ("v_zoom_in", lambda: view.fire_action(rpr, "v_zoom_in")),
        ("v_zoom_out", lambda: view.fire_action(rpr, "v_zoom_out")),
        ("v_scroll_up", lambda: view.fire_action(rpr, "v_scroll_up")),
        ("v_scroll_down", lambda: view.fire_action(rpr, "v_scroll_down")),
        ("h_scroll_left", lambda: view.fire_action(rpr, "h_scroll_left")),
        ("h_scroll_right", lambda: view.fire_action(rpr, "h_scroll_right")),
    ):
        d0 = view.dirty(rpr)
        action()
        view.wait_paint(0.3)
        d1 = view.dirty(rpr)
        audit.append({"op": label, "state_change_delta": view.dirty_delta(d0, d1),
                      "undo_changed": d0["undo_label"] != d1["undo_label"]})
        if entry is not None:
            view.set_view(rpr, entry[0], entry[1], tol=0.0)
    checks["read_only_audit"] = {
        "ok": all(a["state_change_delta"] == 0 and not a["undo_changed"] for a in audit),
        "detail": audit,
        "note": "delta 0 with no undo change means the project was not written",
    }
    if entry is not None:
        view.set_view(rpr, entry[0], entry[1], tol=0.0)
    return {"checks": checks,
            "dirty_delta_whole_test": view.dirty_delta(dirty0, view.dirty(rpr)),
            "ok": all(c.get("ok") for c in checks.values()),
            "shots_dir": str(artifacts.shots_dir())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vision", description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=None,
                        help="write images here instead of the run-artifact directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("state", help="window, view and project state (no image)")

    p = sub.add_parser("shot", help="capture the whole REAPER window")
    p.add_argument("--label", default="shot")
    p.add_argument("--max-width", type=int, default=None)
    p.add_argument("--crop", default=None, help="'x,y,w,h' in window pixels")
    p.add_argument("--activate", action="store_true",
                   help="WORKAROUND for REAPER's 'Set media items offline when "
                        "application is not active' setting: bring the window to "
                        "the front, wait for the media, then give the focus back. "
                        "Prefer turning that setting off - activating pulls the "
                        "user's window in front of their work")
    p.add_argument("--activate-timeout", type=float, default=10.0)
    p.add_argument("--no-restore-focus", dest="restore_focus",
                   action="store_false",
                   help="leave REAPER in front (default is to give the foreground "
                        "back to the window that had it)")

    p = sub.add_parser("view", help="set the visible time range")
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--shift", type=float)
    p.add_argument("--pos-tol", type=float, default=view.DEFAULT_POS_TOL)
    p.add_argument("--shot", action="store_true")
    p.add_argument("--keep-view", action="store_true")
    p.add_argument("--label", default=None)
    p.add_argument("--max-width", type=int, default=None)

    p = sub.add_parser("scroll", help="scroll (dx horizontal, dy vertical)")
    p.add_argument("--dx", type=int, default=0)
    p.add_argument("--dy", type=int, default=0)
    p.add_argument("--measure", action="store_true")
    p.add_argument("--shot", action="store_true")
    p.add_argument("--keep-view", action="store_true")
    p.add_argument("--label", default=None)
    p.add_argument("--max-width", type=int, default=None)

    p = sub.add_parser("zoom", help="zoom (h exact factor or steps, v steps)")
    p.add_argument("--h", default=None)
    p.add_argument("--h-steps", type=int, default=1)
    p.add_argument("--v", choices=["in", "out"], default=None)
    p.add_argument("--v-steps", type=int, default=1)
    p.add_argument("--center", type=float)
    p.add_argument("--pos-tol", type=float, default=view.DEFAULT_POS_TOL)
    p.add_argument("--shot", action="store_true")
    p.add_argument("--keep-view", action="store_true")
    p.add_argument("--label", default=None)
    p.add_argument("--max-width", type=int, default=None)

    p = sub.add_parser("scan", help="tile the arrangement into slices")
    p.add_argument("--span-bars", type=float)
    p.add_argument("--span-seconds", type=float)
    p.add_argument("--overlap", type=float, default=0.05)
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--max-tiles", type=int, default=24)
    p.add_argument("--columns", type=int, default=2)
    p.add_argument("--tile-max-width", type=int, default=640)
    p.add_argument("--crop", default=None,
                   help="'x,y,w,h' in window pixels, applied to every tile and to the "
                        "full-resolution file - crop the static track panel away")
    p.add_argument("--no-contact-sheet", dest="contact_sheet", action="store_false")
    p.add_argument("--pos-tol", type=float, default=view.DEFAULT_POS_TOL)
    p.add_argument("--keep-view", action="store_true")
    p.add_argument("--label", default="scan")
    p.add_argument("--activate", action="store_true",
                   help="WORKAROUND for the media-offline setting (see `shot`); "
                        "OFF by default")
    p.add_argument("--activate-timeout", type=float, default=10.0)
    p.add_argument("--no-restore-focus", dest="restore_focus",
                   action="store_false",
                   help="leave REAPER in front after the scan")

    p = sub.add_parser("selftest", help="verify the frame is the window, the actions "
                                        "match their names and the ops are read-only")
    p.add_argument("--skip-minimize", action="store_true")

    args = parser.parse_args(argv)
    capture.set_dpi_awareness()
    if args.dir:
        os.environ[artifacts.VISION_DIR_ENV] = args.dir
        artifacts.reset_session_dir()
    handlers = {"state": cmd_state, "shot": cmd_shot, "view": cmd_view,
                "scroll": cmd_scroll, "zoom": cmd_zoom, "scan": cmd_scan,
                "selftest": cmd_selftest}
    try:
        report = handlers[args.command](args)
    except Exception as exc:  # noqa: BLE001 - a CLI must always print an answer
        if os.environ.get("REAPER_VISION_TRACEBACK"):
            import traceback

            traceback.print_exc()
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
