"""Capture a window's OWN content, independently of what is active in the OS.

WHY IT IS NOT A SCREEN GRAB
  A screen grab (`BitBlt` from the screen DC) answers "what is on screen", so it
  returns whatever APPLICATION happens to be on top. This module calls
  `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` on REAPER's own window instead:
  the window renders itself, so the image is REAPER's content even while another
  application covers it.

MEASURED (2026-10-17, REAPER 7.53 maximized at 2582x1550, VS Code covering it)
  * occluded window render vs the screen with REAPER really activated:
    8.1/255 mean grey difference, 5.9 % of samples differing by more than 32;
  * the CONTROL - the same occluded render vs the screen while the covering
    application was on top (deliberately activated for the measurement):
    63.5/255, 71.8 %. The comparison can therefore fail, which is what makes the
    8.1 a statement about content and not an artefact of a broken metric;
  * a MINIMIZED window renders a 237x39 / 4-colour artefact, never content:
    `looks_like_window()` is the guard for exactly that signature;
  * recovery is `ShowWindow(SW_SHOWNOACTIVATE)`: measured to work, to stay
    recovered (re-checked after 1 s and 3 s), and not to steal focus in the
    normal case. `SW_RESTORE` is NOT used: measured, it returns a MAXIMIZED
    window to its 683x512 normal size;
  * a view change paints asynchronously, so a single capture can be a stale
    frame. `capture_window()` therefore requires two identical consecutive
    frames (`settle`) before it reports a frame as fresh.

PLATFORM
  Win32 only. On any other platform every function raises
  `CaptureUnavailable` with a typed message instead of returning something
  plausible-looking, and the MCP tools turn that into a typed error.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import time
from ctypes import wintypes

__all__ = [
    "CaptureUnavailable",
    "PLATFORM_OK",
    "set_dpi_awareness",
    "hwnd_from",
    "window_rect",
    "is_minimized",
    "is_visible",
    "foreground_hwnd",
    "set_foreground",
    "activate_window",
    "restore_foreground",
    "show_noactivate",
    "show_minimize",
    "is_maximized",
    "maximize",
    "grab_window",
    "grab_screen_region",
    "image_stats",
    "looks_like_window",
    "capture_window",
]

PLATFORM_OK = os.name == "nt"

SW_MINIMIZE = 6
SW_SHOWNOACTIVATE = 4
SW_MAXIMIZE = 3

# SetWindowPos flags used by `activate_window` (they keep it from moving or
# resizing the window while raising it).
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002

#: Measured: a minimized window renders 237x39. A real one is at least a few
#: hundred pixels wide, so the guard only has to catch the artefact - it is
#: deliberately wide, and the exact numbers are reported either way.
MIN_WINDOW_WIDTH = 600
#: A minimized window sits at -32000 (Win32's off-screen parking position).
MIN_RECT_COORDINATE = -30000


class CaptureUnavailable(RuntimeError):
    """The capture backend cannot run here (not Windows, or user32 missing)."""


if PLATFORM_OK:  # pragma: no branch - the whole module is Win32-only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:  # pragma: no cover - pre-Windows-8.1
        shcore = None

    # Explicit prototypes are not optional: without them ctypes assumes c_int
    # for the handle arguments and a 64-bit HDC/HBITMAP fails with
    # "int too long to convert" (measured).
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int


def _require_platform() -> None:
    if not PLATFORM_OK or "user32" not in globals():
        raise CaptureUnavailable(
            "window capture needs Win32 (user32/gdi32); this host is "
            f"{os.name!r}, so no screenshot can be taken"
        )


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def set_dpi_awareness() -> bool:
    """Make coordinates and captured pixels PHYSICAL.

    Without this the process is DPI-virtualized: the rect comes back in DIPs and
    the captured bitmap in real pixels, so any crop taken from the rect lands in
    the wrong place (measured on a 150 %-scaled 2560x1600 display, where the
    window reported 1707x1067 while the bitmap was 2582x1550).
    """
    _require_platform()
    attempts = []
    if shcore is not None:
        attempts.append(lambda: shcore.SetProcessDpiAwareness(2))
    attempts.append(user32.SetProcessDPIAware)
    for call in attempts:
        try:
            call()
            return True
        except OSError:
            continue
    return False


def hwnd_from(value) -> int:
    """Turn whatever the host API returned into an integer window handle.

    Two shapes have to be survived, both measured:

    * reapy hands back the STRING ``'(HWND)0x00000000000107B0'`` - ``int()`` on
      it raises ``invalid literal for int() with base 10``;
    * a Win32 call whose restype is a pointer type returns **None** when the
      value is 0 (ctypes converts a NULL pointer to None), so
      ``int(user32.GetForegroundWindow())`` raises "int() argument must be a
      string ... not 'NoneType'" in the moment the desktop has no foreground
      window - which is what an otherwise working call looked like when this
      was first measured (2026-10-18).
    """
    if value is None:
        raise ValueError("the host reported no window handle (None)")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if hasattr(value, "value") and isinstance(getattr(value, "value"), int):
        return int(value.value)
    text = str(value)
    match = re.search(r"0x([0-9a-fA-F]+)", text)
    if match:
        return int(match.group(1), 16)
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1))
    raise ValueError(f"cannot read a window handle out of {value!r}")


def window_rect(hwnd: int) -> list[int]:
    """``[left, top, right, bottom]`` in physical screen pixels."""
    _require_platform()
    if not hwnd:
        raise CaptureUnavailable("no window handle: the host did not report one")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise CaptureUnavailable(f"GetWindowRect failed for hwnd {hwnd:#x} "
                                 f"(error {ctypes.get_last_error()})")
    return [rect.left, rect.top, rect.right, rect.bottom]


def is_minimized(hwnd: int) -> bool:
    _require_platform()
    return bool(user32.IsIconic(hwnd))


def is_visible(hwnd: int) -> bool:
    _require_platform()
    return bool(user32.IsWindowVisible(hwnd))


def is_maximized(hwnd: int) -> bool:
    _require_platform()
    return bool(user32.IsZoomed(hwnd))


def foreground_hwnd() -> int:
    """The window with focus, or 0 when the desktop has none.

    `GetForegroundWindow` is declared with a pointer restype, so a zero answer
    arrives as None; reporting 0 instead of raising keeps a tool call alive that
    has nothing to do with focus (measured: an otherwise complete state answer
    died on ``int(None)`` for exactly this).
    """
    _require_platform()
    value = user32.GetForegroundWindow()
    return int(value) if value else 0


def set_foreground(hwnd: int) -> bool:
    _require_platform()
    if not hwnd:
        return False
    return bool(user32.SetForegroundWindow(hwnd))


def activate_window(hwnd: int) -> dict:
    """Bring `hwnd` to the FOREGROUND and say which way actually worked.

    WHY A VISION TOOL NEEDS THIS AT ALL (measured, 2026-10-18; the user's own
    observation, then confirmed through the host API):

      * with the REAPER window in the background, a freshly loaded project's
        audio is NOT loaded: `GetMediaSourceLength` returned **0.0 s**, 1 channel
        and 0 Hz for the mix, while the file was on disk and the project named
        it - and the item consequently draws NO WAVEFORM;
      * **1.09 s after the window was brought to the foreground**, the same call
        returned **89.079 s / 2 channels / 48000 Hz**.

    So the picture of a project that was just opened is not necessarily wrong
    when it is empty - it is a project whose media had not been opened yet. A
    capture that has to be faithful therefore has to be able to activate first,
    then WAIT for the media on the host side (`view.wait_for_media`) - and then
    HAND THE FOREGROUND BACK (`restore_foreground`), because taking the user's
    screen for a screenshot is not acceptable (their requirement, same day: they
    may be typing somewhere else).

    TWO WAYS, IN ORDER, and the first one is known to fail - both live in
    `_foreground_chain`: plain ``SetForegroundWindow``, then the documented
    AttachThreadInput chain. A MINIMIZE + RESTORE pair also worked and is
    deliberately NOT used: it flickers the user's screen and permanently changes
    a maximized window's restore state. `ShowWindow(SW_SHOWNOACTIVATE)` remains
    the way to recover a minimized window for a capture that must not steal focus.

    Returns ``{ok, method, foreground_before, foreground_after, elapsed_s}`` and,
    on failure, ``hint``. Failure is NOT an error of the capture - the frame is
    still valid; what may be missing is the media that activation loads.
    """
    _require_platform()
    if not hwnd:
        raise CaptureUnavailable("no window handle: the host did not report one")
    started = time.perf_counter()
    before = foreground_hwnd()
    info: dict = {"foreground_before": hex(before), "method": None,
                  "foreground_after": None, "ok": False}
    if before == hwnd:
        info.update({"ok": True, "method": "already foreground",
                     "foreground_after": hex(hwnd)})
        info["elapsed_s"] = round(time.perf_counter() - started, 3)
        return info
    method = _foreground_chain(hwnd)
    if method:
        info.update({"ok": True, "method": method})
    else:
        info["method"] = "FAILED"
        info["hint"] = ("Windows refused the foreground change (the foreground "
                        "lock). The capture is still valid - what may stay "
                        "missing is the project's media, which REAPER loads "
                        "when its window comes to the front")
    info["foreground_after"] = hex(foreground_hwnd())
    info["elapsed_s"] = round(time.perf_counter() - started, 3)
    return info


def _foreground_chain(hwnd: int) -> str | None:
    """Foreground in two tries: plain, then the AttachThreadInput chain.

    The plain ``SetForegroundWindow`` is refused by Windows' foreground lock when
    the calling process is not itself foreground: measured, it returned False and
    the foreground window did not change, while the same call succeeded from a
    foreground process. The documented way past it: attach this thread's input to
    the current foreground window's thread, raise the window
    (``BringWindowToTop`` + ``SetWindowPos``), claim the active/focus state, call
    ``SetForegroundWindow``, then detach - measured to work in exactly the state
    where the plain call had just failed, and measured AGAIN in the other
    direction (`restore_foreground`), where the plain call failed for the same
    reason and the chain worked.

    Returns the method that worked, or None.
    """
    if set_foreground(hwnd) and foreground_hwnd() == hwnd:
        return "SetForegroundWindow"
    attached = False
    other = foreground_hwnd()
    mine = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(other, None) if other else 0
    try:
        if theirs and theirs != mine:
            attached = bool(user32.AttachThreadInput(theirs, mine, True))
        user32.BringWindowToTop(hwnd)
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(theirs, mine, False)
    return "AttachThreadInput chain" if foreground_hwnd() == hwnd else None


def restore_foreground(hwnd: int) -> dict:
    """Hand the foreground BACK to the window that had it before the capture.

    The requirement is the user's (2026-10-18): a capture that activates REAPER to
    load its media must not take their screen away - if they were working in
    another window, that window has to come back. Without this, one `activate`
    call leaves REAPER on top and the next thing the user types lands elsewhere.

    Same two-step chain as `activate_window`, in the other direction: measured,
    the plain `SetForegroundWindow` is refused here too (the foreground belongs to
    REAPER at that moment, not to this process), and the AttachThreadInput chain
    puts the previous window back.

    Returns ``{ok, method, foreground_after}``; ``method`` is "not needed" when
    the window already holds the foreground, and "FAILED" with a hint otherwise -
    a caller reports that rather than claiming the user's window came back.
    """
    _require_platform()
    if not hwnd:
        return {"ok": False, "method": "no window", "foreground_after": None,
                "hint": "no previous foreground window was recorded, so none was "
                        "restored"}
    if foreground_hwnd() == hwnd:
        return {"ok": True, "method": "not needed",
                "foreground_after": hex(foreground_hwnd())}
    method = _foreground_chain(hwnd)
    info = {"ok": bool(method), "method": method or "FAILED",
            "foreground_after": hex(foreground_hwnd())}
    if not method:
        info["hint"] = ("Windows refused to return the foreground to the window "
                        "that had it; REAPER stays in front. Nothing about the "
                        "capture is invalid - only the user's window is not back "
                        "where it was")
    return info


def show_noactivate(hwnd: int) -> bool:
    """Recover a minimized window WITHOUT activating it (measured to work)."""
    _require_platform()
    return bool(user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE))


def show_minimize(hwnd: int) -> bool:
    _require_platform()
    return bool(user32.ShowWindow(hwnd, SW_MINIMIZE))


def maximize(hwnd: int) -> bool:
    _require_platform()
    return bool(user32.ShowWindow(hwnd, SW_MAXIMIZE))


def _read_bits(hdc_mem, hbitmap, width: int, height: int):
    """32-bit top-down BGRA out of a memory DC."""
    info = _BitmapInfo()
    info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height          # negative = rows top-down
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0           # BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    rows = gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buffer,
                           ctypes.byref(info), 0)
    return buffer, rows


def grab_window(hwnd: int) -> dict:
    """Render the window's own content into a bitmap (works while occluded).

    Returns ``{ok, rect, size, buf, rows}``; ``buf`` exposes ``.raw`` (BGRA
    bytes, top-down) which is what the imaging helpers consume.
    """
    _require_platform()
    if not hwnd:
        raise CaptureUnavailable("no window handle: the host did not report one")
    left, top, right, bottom = window_rect(hwnd)
    width, height = max(1, right - left), max(1, bottom - top)
    hdc_window = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    previous = gdi32.SelectObject(hdc_mem, hbitmap)
    try:
        ok = user32.PrintWindow(hwnd, hdc_mem, 2)   # 2 = PW_RENDERFULLCONTENT
        buffer, rows = _read_bits(hdc_mem, hbitmap, width, height)
    finally:
        gdi32.SelectObject(hdc_mem, previous)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)
    return {"ok": bool(ok), "rect": [left, top, right, bottom],
            "size": (width, height), "buf": buffer, "rows": rows}


def grab_screen_region(x: int, y: int, width: int, height: int) -> dict:
    """Capture a SCREEN rectangle - the "what is on screen" path.

    Used as the control for `grab_window`, never as the product's image: it
    returns the covering application when the window of interest is behind
    another one (measured: it returned VS Code while REAPER was the target).
    """
    _require_platform()
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    previous = gdi32.SelectObject(hdc_mem, hbitmap)
    try:
        ok = gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, x, y, 0x00CC0020)
        buffer, rows = _read_bits(hdc_mem, hbitmap, width, height)
    finally:
        gdi32.SelectObject(hdc_mem, previous)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
    return {"ok": bool(ok), "rect": [x, y, width, height],
            "size": (width, height), "buf": buffer, "rows": rows}


def image_stats(shot: dict) -> dict:
    """Cheap, deterministic description of a captured frame.

    `nonblack_fraction` and `distinct_sampled_colors` are what makes a failed
    render visible: a blanked or uniform capture is reported instead of being
    saved as if it were a screenshot.
    """
    raw = shot["buf"].raw
    total = len(raw)
    if total == 0:
        return {"sampled_pixels": 0, "nonblack_fraction": 0.0,
                "distinct_sampled_colors": 0, "sha256": "", "sha256_16": ""}
    indices = range(0, total, 4 * 97)
    sampled = list(indices)
    nonblack = sum(1 for i in sampled if raw[i] or raw[i + 1] or raw[i + 2])
    colors = {bytes(raw[i:i + 3]) for i in range(0, total, 4 * 397)}
    digest = hashlib.sha256(raw).hexdigest()
    return {"sampled_pixels": len(sampled),
            "nonblack_fraction": round(nonblack / max(1, len(sampled)), 4),
            "distinct_sampled_colors": len(colors),
            "sha256": digest, "sha256_16": digest[:16]}


def looks_like_window(shot: dict) -> bool:
    """Reject the minimized-window artefact (measured signature: 237x39)."""
    left = shot.get("rect", [0, 0, 0, 0])[0]
    return shot["size"][0] > MIN_WINDOW_WIDTH and left > MIN_RECT_COORDINATE


def _wait_paint(seconds: float) -> None:
    deadline = time.perf_counter() + max(0.0, seconds)
    while time.perf_counter() < deadline:
        time.sleep(0.02)


def capture_window(hwnd: int, *, settle: bool = True, tries: int = 4,
                   delay: float = 0.12, auto_restore: bool = True) -> tuple[dict, dict]:
    """One trustworthy frame of `hwnd`, plus what had to be done to get it.

    The two guards are the feature:

    * a MINIMIZED window is un-minimized without activation, captured, and put
      back the way it was found (``minimized_before`` / ``minimized_after`` say
      what happened; ``auto_restored`` says whether recovery worked);
    * a view change paints asynchronously, so two CONSECUTIVE IDENTICAL frames
      are required before a frame counts as fresh. A stale frame read as fresh
      is the failure this tool must not have - it would make an agent reason
      about a picture of the previous bar.
    """
    _require_platform()
    info: dict = {
        "minimized_before": is_minimized(hwnd),
        "maximized": is_maximized(hwnd),
        "visible": is_visible(hwnd),
        "foreground_before": hex(foreground_hwnd()),
        "auto_restored": False,
        "focus_restored": None,
        "minimized_after": None,
        "rect": window_rect(hwnd),
    }
    if info["minimized_before"] and auto_restore:
        show_noactivate(hwnd)
        _wait_paint(0.7)
        info["auto_restored"] = not is_minimized(hwnd)
        previous_foreground = int(info["foreground_before"], 16)
        # 0 means "the desktop had no foreground window", which is not a window
        # to hand back to.
        if previous_foreground and foreground_hwnd() != previous_foreground:
            info["focus_restored"] = bool(set_foreground(previous_foreground))
            _wait_paint(0.2)

    attempts: list[dict] = []
    shot: dict | None = None
    previous_digest: str | None = None
    started = time.perf_counter()
    rounds = tries if settle else 1
    for attempt in range(1, rounds + 1):
        t0 = time.perf_counter()
        shot = grab_window(hwnd)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        digest = hashlib.sha256(shot["buf"].raw).hexdigest()[:12]
        attempts.append({"attempt": attempt, "sha": digest, "ms": elapsed_ms})
        if previous_digest is not None and digest == previous_digest:
            info["settle"] = {"settled": True, "attempts": attempt}
            break
        previous_digest = digest
        if attempt < rounds:
            time.sleep(delay)
    else:
        info["settle"] = {"settled": False, "attempts": rounds}
    info["settle"]["history"] = attempts
    info["capture_ms_total"] = round((time.perf_counter() - started) * 1000, 1)
    info["size"] = list(shot["size"])
    info["stats"] = image_stats(shot)
    info["looks_like_window"] = looks_like_window(shot)
    if info["minimized_before"] and auto_restore:
        show_minimize(hwnd)
        _wait_paint(0.3)
        info["minimized_after"] = is_minimized(hwnd)
    return shot, info
