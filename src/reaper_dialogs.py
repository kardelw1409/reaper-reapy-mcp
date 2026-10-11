"""Non-blocking REAPER dialog guard (provenance: C:/REAPER T-01, measured twice).

A REAPER **modal blocks all reapy communication**: every call hangs and the
caller cannot even learn why. The trigger recorded in the reference corpus
(`prompts/vital_sound_design/knowledge/TROUBLESHOOTING_MAP.md`, T-01) is the
``render_project`` shape that sets ``RENDER_BOUNDSFLAG=3`` and fires
``perform_action(41824)`` - REAPER then raises the modal **"Render Error /
Nothing to render!"** and freezes for the bridge.

Two primitives, and the reason for each (both measured in the reference work):

- **read** with ``SendMessageTimeoutW(WM_GETTEXT, SMTO_ABORTIFHUNG)`` - never
  ``GetWindowTextW``, which SENDS ``WM_GETTEXT`` and does not return while the
  target thread sits in its own modal loop. Measured: that mistake wedged
  REAPER with the box up and left a partial file on disk while the rescuer
  deadlocked on the window read.
- **act** with ``PostMessageW(BM_CLICK)`` - never ``SendMessage``, for the same
  reason: the click must be queued to the box's own thread, not executed on the
  rescuer's.

Policy (deliberately narrow, fail-closed): only windows of class ``#32770``
owned by the REAPER process that match a **known** box are answered:

- "Render Error" / "Nothing to render!" -> the **OK** button (acknowledge);
- "Render Incomplete!" / "Save incomplete rendered files?" -> **No** (discard
  the partial file; "Yes" would publish a partial render as a finished one,
  which silently invalidates every measurement made with it).

Everything else is **reported, never clicked** - a guard that guesses is worse
than no guard.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Callable, Iterable

__all__ = [
    "available",
    "find_reaper_pid",
    "list_dialogs",
    "classify",
    "choose_button",
    "answer",
    "sweep_render_boxes",
    "Watcher",
    "start_watcher",
    "watcher_state",
    "NEVER_DISMISS",
]

_DIALOG_CLASS = "#32770"
_REAPER_CLASS = "REAPERwnd"

_WM_GETTEXT = 0x000D
_BM_CLICK = 0x00F5
_SMTO_ABORTIFHUNG = 0x0002

#: Standard button ids as fallbacks when the text is unusable. Not the primary
#: selector: the live box measured in this workspace has its OK button at id 2
#: (``id=2, text='OK'``), so **text decides** and the ids are only a fallback.
_IDOK, _IDCANCEL, _IDYES, _IDNO = 1, 2, 6, 7

#: Box kinds and their answers. A kind is recognised by a needle in the window
#: title OR in the box's own message text; the two are kept apart because
#: REAPER puts "Render Error" in the title and "Nothing to render!" in the text.
_KINDS: tuple[dict[str, Any], ...] = (
    {
        "kind": "render_error",
        "title_needles": ("render error",),
        "text_needles": ("nothing to render",),
        "answer": "ok",
        "why": (
            "an error box: it must be acknowledged, and it names a render that "
            "did not happen - no partial file exists and none may be published"
        ),
    },
    {
        "kind": "render_incomplete",
        "title_needles": ("render incomplete",),
        "text_needles": ("save incomplete", "incomplete rendered"),
        "answer": "no",
        "why": (
            "a question: 'Yes' writes the partial file out as if it were a "
            "finished render, which invalidates every measurement made with it"
        ),
    },
    {
        "kind": "reascript_error",
        "title_needles": ("reascript error",),
        "text_needles": ("deferred script execution error",),
        "answer": "close",
        "why": (
            "an informational script-error box; while it is up REAPER answers no "
            "API call at all (measured: every reapy call hangs), so it is closed"
        ),
    },
)

#: Titles/messages that must NEVER be dismissed automatically (proven list from
#: the reference dialog guard, `Scripts/sound_design/vital/bridge/dialogs.py`):
#: a question about the user's work cannot be answered by a robot, and guessing
#: is worse than stopping. Explicit kinds above take precedence, because their
#: answer is defined by this module's policy rather than guessed.
NEVER_DISMISS = ("save", "overwrite", "replace", "do you want", "are you sure")

#: Generic REAPER error boxes (e.g. "Error opening preset") are dismissed with
#: OK/Close. Matched only for the REAPER process, only for error-shaped titles,
#: and only when no NEVER_DISMISS needle is present.
_GENERIC_ERROR_TITLE = ("error", "warning", "failed")

_ANSWER_BUTTON_TEXT = {
    "ok": ("ok",),
    "close": ("close", "ok"),
    "no": ("no", "&no"),
    "yes": ("yes", "&yes"),
}
_ANSWER_BUTTON_IDS = {"ok": (_IDOK, _IDCANCEL), "close": (_IDCANCEL, _IDOK), "no": (_IDNO,), "yes": (_IDYES,)}


def available() -> bool:
    """True when the Win32 primitives are usable in this process."""
    return sys.platform == "win32"


def _u32():
    lib = ctypes.WinDLL("user32", use_last_error=True)
    lib.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.SendMessageTimeoutW.restype = ctypes.c_void_p
    lib.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.EnumWindows.restype = ctypes.c_int
    lib.EnumChildWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.EnumChildWindows.restype = ctypes.c_int
    lib.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.GetClassNameW.restype = ctypes.c_int
    lib.IsWindowVisible.argtypes = [ctypes.c_void_p]
    lib.IsWindowVisible.restype = ctypes.c_int
    lib.GetDlgCtrlID.argtypes = [ctypes.c_void_p]
    lib.GetDlgCtrlID.restype = ctypes.c_int
    lib.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    lib.GetWindowThreadProcessId.restype = ctypes.c_uint
    lib.GetDlgItem.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.GetDlgItem.restype = ctypes.c_void_p
    lib.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    lib.PostMessageW.restype = ctypes.c_int
    return lib


_ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _safe_title(u32, hwnd: int) -> str:
    """Read a window title WITHOUT blocking, even if that window is hung."""
    buf = ctypes.create_unicode_buffer(1024)
    res = ctypes.c_size_t(0)
    ok = u32.SendMessageTimeoutW(
        ctypes.c_void_p(hwnd), _WM_GETTEXT, 1024,
        ctypes.c_void_p(ctypes.addressof(buf)), _SMTO_ABORTIFHUNG, 200,
        ctypes.byref(res),
    )
    return buf.value if ok else ""


def _class_of(u32, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(ctypes.c_void_p(hwnd), ctypes.c_void_p(ctypes.addressof(buf)), 256)
    return buf.value


def _pid_of(u32, hwnd: int) -> int:
    pid = ctypes.c_uint(0)
    u32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _children(u32, hwnd: int) -> list[int]:
    out: list[int] = []

    def cb(ch, _l):
        out.append(ch)
        return 1

    u32.EnumChildWindows(ctypes.c_void_p(hwnd), _ENUMPROC(cb), None)
    return out


def _windows(u32) -> list[int]:
    out: list[int] = []

    def cb(ch, _l):
        out.append(ch)
        return 1

    u32.EnumWindows(_ENUMPROC(cb), None)
    return out


def find_reaper_pid(inventory: Iterable[dict[str, Any]] | None = None) -> int | None:
    """The pid owning REAPER's main window (`REAPERwnd`), or None when absent.

    The boxes are only answered when their pid matches this, so a dialog that
    belongs to another application can never be clicked by mistake.
    """
    if not available():
        return None
    if inventory is None:
        u32 = _u32()
        rows = [
            {"hwnd": h, "class": _class_of(u32, h), "pid": _pid_of(u32, h)}
            for h in _windows(u32)
        ]
    else:
        rows = list(inventory)
    for row in rows:
        if str(row.get("class")) == _REAPER_CLASS and row.get("pid"):
            return int(row["pid"])
    return None


def list_dialogs(*, reaper_only: bool = False) -> list[dict[str, Any]]:
    """A non-blocking inventory of dialog windows (class ``#32770``).

    Every read goes through ``SendMessageTimeoutW(SMTO_ABORTIFHUNG)``, so a box
    whose thread is blocked in its own modal loop is *reported* rather than
    deadlocking this call - which is the whole point: the caller that needs this
    inventory is the caller REAPER is currently ignoring.
    """
    if not available():
        return []
    u32 = _u32()
    reaper_pid = find_reaper_pid() if reaper_only else None
    rows: list[dict[str, Any]] = []
    for hwnd in _windows(u32):
        if _class_of(u32, hwnd) != _DIALOG_CLASS:
            continue
        pid = _pid_of(u32, hwnd)
        if reaper_only and pid != reaper_pid:
            continue
        title = _safe_title(u32, hwnd)
        texts: list[str] = []
        buttons: list[dict[str, Any]] = []
        for child in _children(u32, hwnd):
            text = _safe_title(u32, child)
            klass = _class_of(u32, child)
            if not text:
                continue
            cid = int(u32.GetDlgCtrlID(ctypes.c_void_p(child)))
            if klass == "Button":
                buttons.append({"id": cid, "text": text, "hwnd": f"0x{child:X}"})
            else:
                texts.append(text)
        rows.append(
            {
                "hwnd": f"0x{hwnd:X}",
                "pid": pid,
                "title": title,
                "visible": bool(u32.IsWindowVisible(ctypes.c_void_p(hwnd))),
                "texts": texts,
                "buttons": buttons,
            }
        )
    return rows


def classify(dialog: dict[str, Any]) -> dict[str, Any]:
    """Which known box this is, and the answer policy - pure, testable.

    A needle in the title OR in the message text marks the kind. Then a generic
    pass handles REAPER's own error-shaped boxes ("Error opening preset"), and
    anything carrying a NEVER_DISMISS needle is left alone. An unknown box
    answers ``{"kind": "unknown", "answer": None}`` and MUST NOT be clicked.
    """
    title = str(dialog.get("title") or "").lower()
    texts = [str(item).lower() for item in dialog.get("texts") or []]
    haystacks = [title] + texts
    blocked = any(
        needle in hay for hay in haystacks for needle in NEVER_DISMISS
    )
    for spec in _KINDS:
        title_hit = any(needle in title for needle in spec["title_needles"])
        text_hit = any(
            needle in hay for hay in haystacks for needle in spec["text_needles"]
        )
        if title_hit or text_hit:
            return {
                "kind": spec["kind"],
                "answer": spec["answer"],
                "why": spec["why"],
                "matched": "title" if title_hit else "text",
            }
    if not blocked and any(needle in title for needle in _GENERIC_ERROR_TITLE):
        return {
            "kind": "reaper_error",
            "answer": "close",
            "why": (
                "a REAPER error-shaped box with no question in it; while such a "
                "box is up REAPER answers no API call, so it is acknowledged"
            ),
            "matched": "title",
        }
    if blocked:
        return {
            "kind": "question",
            "answer": None,
            "why": (
                "a question about the user's own work (save/overwrite/are you "
                "sure); it is reported and left alone by policy"
            ),
            "matched": "policy",
        }
    return {
        "kind": "unknown",
        "answer": None,
        "why": "not a box this guard knows; it is reported and left alone",
        "matched": None,
    }


def choose_button(
    dialog: dict[str, Any], answer: str, *, inventory: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """The button to click for ``answer``: **by text first**, ids as fallback.

    Text first because the live REAPER box in this workspace was measured with
    its OK button at control id **2** (the conventional ``IDCANCEL``), so a
    purely id-based choice would either miss the button or click the wrong one.
    """
    wanted_texts = _ANSWER_BUTTON_TEXT.get(answer, ())
    buttons = [b for b in dialog.get("buttons") or [] if b.get("hwnd")]
    for button in buttons:
        if str(button.get("text") or "").strip().lower() in wanted_texts:
            return button
    wanted_ids = _ANSWER_BUTTON_IDS.get(answer, ())
    for button in buttons:
        if int(button.get("id") or -1) in wanted_ids:
            return button
    if inventory is not None:  # injected inventory: no live handles to fall back to
        return None
    u32 = _u32()
    for cid in wanted_ids:
        handle = u32.GetDlgItem(ctypes.c_void_p(int(str(dialog["hwnd"]), 16)), cid)
        if handle:
            return {"id": cid, "text": "", "hwnd": f"0x{handle:X}"}
    return None


_WM_COMMAND = 0x0111
_WM_CLOSE = 0x0010


def _is_alive(hwnd: int) -> bool:
    """Does the window still exist? A window-manager read, so it cannot block."""
    if not available():
        return False
    u32 = _u32()
    u32.IsWindow.argtypes = [ctypes.c_void_p]
    u32.IsWindow.restype = ctypes.c_int
    return bool(u32.IsWindow(ctypes.c_void_p(hwnd)))


#: How long to wait before asking whether a posted answer took effect.
#: MEASURED 2026-10-11: the box closed, but an immediate ``IsWindow`` still said
#: alive - REAPER processes the posted message in its own loop, so the check must
#: allow the round trip. Without the settle, every mechanism looked like a
#: failure while the first one had already worked.
_SETTLE_S = 0.4


def _closed_after(hwnd: int, *, settle_s: float = _SETTLE_S) -> bool:
    import time as _time

    _time.sleep(settle_s)
    return not _is_alive(hwnd)


def answer(dialog: dict[str, Any], *, inventory: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Answer a known box, with a **verified** ladder of mechanisms.

    Measured on this workspace's live box (2026-10-11): ``PostMessage(BM_CLICK)``
    to the button was posted and the box **stayed up**; ``PostMessage(WM_COMMAND)``
    with the button's own control id dismissed it. The ladder therefore does what
    the measurement showed, checking ``IsWindow`` after each step instead of
    trusting the post:

    1. ``BM_CLICK`` on the button (the conventional path, and it works for
       ordinary message boxes);
    2. ``WM_COMMAND`` with the button's id to the dialog;
    3. ``WM_CLOSE`` to the dialog.

    ``steps`` records every attempt and ``dismissed_by`` names the one that
    worked - or ``None``, because "the box did not go away" must be a visible
    outcome rather than a silent success.
    """
    verdict = classify(dialog)
    row: dict[str, Any] = {
        "hwnd": dialog.get("hwnd"),
        "title": dialog.get("title"),
        "kind": verdict["kind"],
        "answer": verdict["answer"],
        "clicked": False,
        "button": None,
        "dismissed_by": None,
        "steps": [],
        "why": verdict["why"],
    }
    if verdict["answer"] is None:
        return row
    button = choose_button(dialog, verdict["answer"], inventory=inventory)
    if button is None:
        row["why"] = (
            f"{verdict['why']}; the {verdict['answer']} button was not found, so nothing was posted"
        )
        return row
    row["button"] = button
    if inventory is not None:
        # injecting the inventory means the caller is asking for the **decision**
        # only (tests, dry sweeps): no live handle exists to post to, and a
        # fabricated `clicked` would be a claim nothing measured
        row["decision_only"] = True
        row["would_click"] = {"id": button.get("id"), "text": button.get("text")}
        return row
    u32 = _u32()
    dialog_hwnd = int(str(dialog["hwnd"]), 16)
    button_hwnd = int(str(button["hwnd"]), 16)
    if not _is_alive(dialog_hwnd):
        row["already_gone"] = True
        row["why"] = f"{verdict['why']}; the box was already gone when the sweep reached it"
        return row

    posted = u32.PostMessageW(ctypes.c_void_p(button_hwnd), _BM_CLICK, 0, 0)
    if _closed_after(dialog_hwnd):
        row.update(clicked=True, dismissed_by="BM_CLICK")
        return row
    row["steps"].append({"method": "BM_CLICK", "posted": int(bool(posted)), "alive_after": True})

    cmd = int(button.get("id") or 0) & 0xFFFF
    posted = u32.PostMessageW(ctypes.c_void_p(dialog_hwnd), _WM_COMMAND, cmd, 0)
    if _closed_after(dialog_hwnd):
        row.update(clicked=True, dismissed_by="WM_COMMAND")
        return row
    row["steps"].append(
        {"method": "WM_COMMAND", "id": cmd, "posted": int(bool(posted)), "alive_after": True}
    )

    posted = u32.PostMessageW(ctypes.c_void_p(dialog_hwnd), _WM_CLOSE, 0, 0)
    if _closed_after(dialog_hwnd):
        row.update(clicked=True, dismissed_by="WM_CLOSE")
        return row
    row["steps"].append({"method": "WM_CLOSE", "posted": int(bool(posted)), "alive_after": True})
    row["why"] = f"{verdict['why']}; every posted mechanism left the box up"
    return row


def sweep_render_boxes(
    *, do: bool = True, dialogs: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Answer every REAPER render box currently up (or report them when ``do`` is False).

    The sweep is scoped to REAPER's own windows: a dialog of another process is
    never touched.
    """
    if dialogs is None:
        if not available():
            return []
        dialogs = list_dialogs(reaper_only=True)
        injected = False
    else:
        injected = True
    rows: list[dict[str, Any]] = []
    for dialog in dialogs:
        verdict = classify(dialog)
        if verdict["kind"] in ("unknown", "question"):
            rows.append(
                {
                    "hwnd": dialog.get("hwnd"),
                    "title": dialog.get("title"),
                    "kind": verdict["kind"],
                    "clicked": False,
                    "why": verdict["why"],
                }
            )
            continue
        if not do:
            rows.append(
                {
                    "hwnd": dialog.get("hwnd"),
                    "title": dialog.get("title"),
                    "kind": verdict["kind"],
                    "answer": verdict["answer"],
                    "clicked": False,
                    "why": "report only",
                }
            )
            continue
        rows.append(answer(dialog, inventory=[] if injected else None))
    return rows


# ---------------------------------------------------------------------------
# The watcher: dismiss REAPER's error boxes before a person has to
# ---------------------------------------------------------------------------
# Provenance: the reference dialog guard
# (`C:/REAPER/Scripts/sound_design/vital/bridge/dialogs.py`) starts a background
# daemon that answers REAPER's boxes, and its docstring states the design rule
# this file follows: **nothing on the polling path may block**. The same rule was
# re-measured here the hard way - a rescuer that imported reapy wedged on the
# machine registration socket while REAPER held a modal.

_WATCH_INTERVAL_S = 0.35


class Watcher:
    """A daemon thread that answers known REAPER boxes and records what it did.

    The thread never raises (an exception in a guard thread is invisible and the
    guard is the thing that is supposed to be reliable), and it never blocks: its
    only primitives are enumeration, timed-out message reads and posted messages.
    """

    def __init__(self, *, interval_s: float = _WATCH_INTERVAL_S) -> None:
        import threading

        self.interval_s = float(interval_s)
        self.actions: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.polls = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reaper-dialog-watcher", daemon=True)

    def start(self) -> "Watcher":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:  # pragma: no cover - timing dependent, exercised live
        while not self._stop.is_set():
            try:
                self.polls += 1
                for row in sweep_render_boxes(do=True):
                    if row.get("clicked"):
                        self.actions.append({"at": __import__("time").time(), **row})
            except Exception as exc:  # noqa: BLE001 - recorded, never raised
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.interval_s)


_WATCHER: Watcher | None = None


def start_watcher(*, interval_s: float = _WATCH_INTERVAL_S) -> Watcher | None:
    """Start the process-wide watcher (idempotent). No-op where Win32 is absent."""
    global _WATCHER
    if not available():
        return None
    if _WATCHER is not None and _WATCHER.running:
        return _WATCHER
    _WATCHER = Watcher(interval_s=interval_s).start()
    return _WATCHER


def watcher_state() -> dict[str, Any]:
    """What the watcher has done, so 'the guard is running' is checkable.

    The shape is the same whether or not the watcher ever started: a caller that
    reads ``dismissed`` must not get a KeyError for the case "nothing was
    started", which is exactly the case worth reporting.
    """
    if _WATCHER is None:
        return {"started": False, "running": False, "polls": 0, "dismissed": [], "errors": []}
    return {
        "started": True,
        "running": _WATCHER.running,
        "polls": _WATCHER.polls,
        "dismissed": list(_WATCHER.actions),
        "errors": list(_WATCHER.errors),
    }


def _main(argv: list[str]) -> int:  # pragma: no cover - diagnostic entry point
    import json

    mode = argv[1] if len(argv) > 1 else "list"
    if mode == "dismiss":
        print(json.dumps(sweep_render_boxes(do=True), indent=2))
        return 0
    print(json.dumps(list_dialogs(reaper_only="--all" not in argv), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv))
