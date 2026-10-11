"""Offline policy tests for the REAPER dialog guard (`src/reaper_dialogs.py`).

The guard exists because a REAPER modal blocks ALL reapy communication, so the
caller cannot even learn why it is stuck (measured twice in this workspace and
recorded as T-01 in the reference corpus). These tests pin the **policy** - what
is recognised, what is answered, and what is never touched - using injected
window inventories, so they need no live window and no REAPER.

The one thing they cannot test offline is the click itself; that is exercised
live (`answer()` posts, then re-checks the window), and the ladder's shape is
asserted here through the decision-only path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import reaper_dialogs as RD  # noqa: E402


def _dialog(title: str, *texts: str, buttons: list[tuple[int, str]] | None = None) -> dict:
    return {
        "hwnd": "0xDEAD",
        "pid": 4242,
        "title": title,
        "visible": True,
        "texts": list(texts),
        "buttons": [
            {"id": cid, "text": text, "hwnd": f"0x{cid:X}"} for cid, text in (buttons or [])
        ],
    }


# ---------------------------------------------------------------------------
# Classification: the measured boxes of 2026-10-11
# ---------------------------------------------------------------------------


def test_render_error_box_is_acknowledged():
    """The live box: title 'Render Error', text 'Nothing to render!', one OK."""
    verdict = RD.classify(_dialog("Render Error", "Nothing to render!", buttons=[(2, "OK")]))
    assert verdict["kind"] == "render_error"
    assert verdict["answer"] == "ok"


def test_render_incomplete_is_answered_no():
    """'Yes' would publish a partial file as a finished render - the measured
    failure mode that silently invalidates every comparison made with it."""
    verdict = RD.classify(
        _dialog("Render Incomplete!", "Save incomplete rendered files?", buttons=[(6, "Yes"), (7, "No")])
    )
    assert verdict["kind"] == "render_incomplete"
    assert verdict["answer"] == "no"


def test_reascript_error_is_closed():
    """The traceback-from-a-script box: up => every reapy call hangs."""
    verdict = RD.classify(
        _dialog("ReaScript Error", "Deferred script execution error", buttons=[(1, "Close")])
    )
    assert verdict["kind"] == "reascript_error"
    assert verdict["answer"] == "close"


def test_generic_reaper_error_is_acknowledged():
    verdict = RD.classify(_dialog("Error opening preset", "Something went wrong"))
    assert verdict["kind"] == "reaper_error"
    assert verdict["answer"] == "close"


@pytest.mark.parametrize(
    "title",
    ["Save project?", "Overwrite file?", "Replace existing?", "Do you want to continue?", "Are you sure?"],
)
def test_questions_about_the_users_work_are_never_answered(title: str):
    """The proven NEVER_DISMISS policy: guessing an answer to a question about
    someone's work is worse than stopping."""
    verdict = RD.classify(_dialog(title))
    assert verdict["answer"] is None, verdict
    assert verdict["kind"] == "question"


def test_an_unknown_box_is_reported_not_clicked():
    verdict = RD.classify(_dialog("Add FX to: Track 1", "Filter:", "List1"))
    assert verdict["kind"] == "unknown"
    assert verdict["answer"] is None


# ---------------------------------------------------------------------------
# Button choice: text first, ids as the fallback
# ---------------------------------------------------------------------------


def test_button_is_chosen_by_text_even_when_the_id_is_the_conventional_cancel():
    """MEASURED: the live REAPER box carries its OK at control id 2, which is
    the conventional IDCANCEL - an id-only choice would click the wrong thing."""
    dialog = _dialog("Render Error", "Nothing to render!", buttons=[(2, "OK")])
    button = RD.choose_button(dialog, "ok", inventory=[])
    assert button is not None and button["text"] == "OK"


def test_button_falls_back_to_the_id_when_the_text_is_unreadable():
    dialog = _dialog("Render Error", "Nothing to render!", buttons=[(2, "")])
    button = RD.choose_button(dialog, "ok", inventory=[])
    assert button is not None and button["id"] == 2


def test_close_answer_prefers_the_close_button_over_ok():
    dialog = _dialog(
        "ReaScript Error",
        "Deferred script execution error",
        buttons=[(1, "Close"), (2, "End script"), (3, "Go to error")],
    )
    button = RD.choose_button(dialog, "close", inventory=[])
    assert button is not None and button["text"] == "Close"


# ---------------------------------------------------------------------------
# Sweeps with an injected inventory: decisions only, never fabricated clicks
# ---------------------------------------------------------------------------


def test_injected_sweep_decides_without_claiming_a_click():
    rows = RD.sweep_render_boxes(
        do=True,
        dialogs=[
            _dialog("Render Error", "Nothing to render!", buttons=[(2, "OK")]),
            _dialog("Save project?", buttons=[(6, "Yes"), (7, "No")]),
        ],
    )
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["render_error"]["decision_only"] is True
    assert by_kind["render_error"]["clicked"] is False
    assert by_kind["render_error"]["would_click"]["text"] == "OK"
    assert by_kind["question"]["clicked"] is False
    assert by_kind["question"]["why"]


def test_report_only_mode_clicks_nothing():
    rows = RD.sweep_render_boxes(
        do=False, dialogs=[_dialog("Render Error", "Nothing to render!", buttons=[(2, "OK")])]
    )
    assert all(row["clicked"] is False for row in rows)
    assert rows[0]["why"] == "report only"


def test_never_dismiss_policy_is_the_proven_list():
    """The list is copied from the reference guard; if someone shortens it, the
    guard starts answering questions about the user's work - so it is pinned."""
    assert set(RD.NEVER_DISMISS) == {"save", "overwrite", "replace", "do you want", "are you sure"}


def test_watcher_state_is_checkable_even_before_start():
    state = RD.watcher_state()
    assert "started" in state and "dismissed" in state


def test_module_imports_nothing_that_can_block():
    """The rescuer must be importable while REAPER is modal-blocked.

    The measured trap: `src/utils/__init__.py` imports `item_utils`, which
    imports `reapy`, whose machine registration performs a **blocking socket
    receive**. A guard reached through that package cannot start when it is
    needed. This test reads the module source and pins its imports.
    """
    source = (SRC / "reaper_dialogs.py").read_text(encoding="utf-8")
    for banned in ("import reapy", "from reapy", "import utils", "from utils"):
        assert banned not in source, f"{banned!r} would make the guard unimportable while REAPER is blocked"
