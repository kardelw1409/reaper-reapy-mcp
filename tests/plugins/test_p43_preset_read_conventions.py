"""P43, second half - reapy returns the ARGUMENT LIST, and the preset layer read the wrong slots.

MEASURED, live, on the first call these tools ever received (2026-10-18, dist API):

    TrackFX_GetPreset(track.id, 0, "", 1024)
        -> [1, '(MediaTrack*)0x0000000005D9AAA0', 0, '', 1024]
    TrackFX_GetPresetIndex(track.id, 0, 0)
        -> [-1, '(MediaTrack*)0x0000000005D9AAA0', 0, 4]
    TrackFX_GetUserPresetFilename(track.id, 0, "", 1024)
        -> ['(MediaTrack*)0x0000000005D9AAA0', 0, 'C:\\REAPER\\presets\\vst3-Vital.ini', 1024]
    GetTrackStateChunk(track.id, "", 4100000, False)
        -> [1, '(MediaTrack*)0x0000000005D9AAA0', '<TRACK\\n...', 4100000, False]

The container is the WHOLE argument list with out-parameters updated in place, retval first.
The layer's selectors were written against a guessed `(retval, buffer)` shape, and the first live
call showed what that cost: `current` came back as the track's POINTER, `host_preset_count` as `0`
(the FX-element echo) while the host was answering **4**, and `disagreement` fired against a file
that agreed. `bool(result)` on a list is also True whatever the host said, so a refused
`TrackFX_SetPreset` reported `applied: true`.

These tests run the controller against containers copied from that measurement: no host, no
connection.
"""

from __future__ import annotations

import importlib
import json
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

POINTER = "(MediaTrack*)0x0000000005D9AAA0"


def preset_controller():
    """A controller whose host is a stub: `__new__` skips `reapy.connect()` (no host offline)."""
    module = importlib.import_module("controllers.preset_controller")
    instance = module.PresetController.__new__(module.PresetController)
    instance._track = lambda track_index: types.SimpleNamespace(id=POINTER)
    return instance, module


def fake_host(module, monkeypatch, **answers):
    """Replace the three preset reads (and any write) with the measured containers."""
    api = module.reapy.reascript_api
    for name, answer in answers.items():
        if callable(answer):
            monkeypatch.setattr(api, name, answer, raising=False)
        else:
            monkeypatch.setattr(api, name, lambda *args, _a=answer: _a, raising=False)
    return api


def measured_reads(preset="", index=-1, count=4, path=None):
    return {
        "TrackFX_GetPreset": [1, POINTER, 0, preset, 1024],
        "TrackFX_GetPresetIndex": [index, POINTER, 0, count],
        "TrackFX_GetUserPresetFilename": [POINTER, 0, path, 1024],
    }


# ---------------------------------------------------------------------------
# The wrong slots, one by one
# ---------------------------------------------------------------------------


def test_the_echo_of_the_track_pointer_is_never_an_answer(monkeypatch):
    """`current` was the track's pointer: the input echo must be identified as an echo."""
    controller, module = preset_controller()
    fake_host(module, monkeypatch, **measured_reads(path=None))
    name, shape = controller._current_preset(0, 0)
    assert name is None, "the measured buffer is EMPTY - that is 'no preset is current'"
    assert POINTER not in shape and "echo" in shape, shape
    path, path_shape = controller._user_preset_file(0, 0)
    assert path is None, "a null path in the container is not the pointer"
    assert "echo" in path_shape, path_shape


def test_the_count_comes_from_the_out_parameter_not_the_fx_echo(monkeypatch):
    """The defect in one line: `[-1, pointer, 0, 4]` means index -1 and FOUR presets."""
    controller, module = preset_controller()
    fake_host(module, monkeypatch, TrackFX_GetPresetIndex=[-1, POINTER, 0, 4])
    index, count, layout = controller._preset_index(0, 0)
    assert (index, count) == (-1, 4), layout
    # the fx index is not a preset count, whichever slot it sits in
    fake_host(module, monkeypatch, TrackFX_GetPresetIndex=[7, POINTER, 3, 4])
    index, count, layout = controller._preset_index(0, 0)
    assert (index, count) == (7, 4), layout
    assert layout == "list:list[int,echo,int,int]", layout


def test_a_container_that_is_not_the_measured_shape_reports_no_number(monkeypatch):
    """A guessed slot is what caused this: an unrecognised container answers None, not a value."""
    controller, module = preset_controller()
    fake_host(module, monkeypatch, TrackFX_GetPresetIndex=lambda *a: [POINTER, 0, 4])
    index, count, layout = controller._preset_index(0, 0)
    assert (index, count) == (None, None), layout
    assert layout == "list:list[echo,int,int]", layout


def test_the_chunk_is_the_text_element_also_when_it_is_short(monkeypatch):
    """`len > 100` fell through to the echo: a short chunk used to come back as the pointer."""
    controller, module = preset_controller()
    short = "<TRACK\nNAME \"x\"\n>\n"
    fake_host(module, monkeypatch, GetTrackStateChunk=[1, POINTER, short, 4100000, False])
    assert controller._read_chunk(0) == short
    fake_host(module, monkeypatch, GetTrackStateChunk=[None, POINTER, 4100000, False])
    with pytest.raises(RuntimeError) as bad:
        controller._read_chunk(0)
    assert "list[NoneType,echo,int,bool]" in str(bad.value)


# ---------------------------------------------------------------------------
# The write calls: a list is truthy, so `bool(result)` was always True
# ---------------------------------------------------------------------------


def test_a_refused_write_is_not_reported_as_applied(monkeypatch):
    """`TrackFX_SetPreset` -> `[False, pointer, 0]`: the retval is the FIRST element."""
    controller, module = preset_controller()
    reads = measured_reads()
    reads["TrackFX_SetPreset"] = lambda *a: [False, POINTER, 0]
    fake_host(module, monkeypatch, **reads)
    answer = controller.set_fx_preset(0, 0, name="BassHouse")
    assert answer["applied"] is False and answer["status"] == "error", answer
    assert answer["verified"] is False, answer
    assert answer["call_layout"] == "list[bool,echo,int]", answer


def test_an_understood_yes_is_verified_against_the_read_back(monkeypatch):
    controller, module = preset_controller()
    reads = measured_reads(preset="BassHouse")
    reads["TrackFX_SetPreset"] = lambda *a: [True, POINTER, 0]
    fake_host(module, monkeypatch, **reads)
    answer = controller.set_fx_preset(0, 0, name="BassHouse")
    assert answer["applied"] is True and answer["verified"] is True, answer
    assert answer["after"] == {"name": "BassHouse", "index": -1, "count": 4}, answer


def test_an_unrecognised_answer_is_never_folded_into_a_yes(monkeypatch):
    controller, module = preset_controller()
    reads = measured_reads()
    reads["TrackFX_NavigatePresets"] = lambda *a: None
    fake_host(module, monkeypatch, **reads)
    answer = controller.navigate_fx_presets(0, 0, step=1)
    assert answer["moved"] is None and answer["status"] == "error", answer
    assert "not a shape this layer understands" in answer["message"], answer


def test_a_bare_scalar_retval_is_the_hosts_answer(monkeypatch):
    """MEASURED (1b, 2026-10-18): `TrackFX_SetPreset` (no out-parameters) answers the bare 1.

    The live call applied `BassHouse` for real (`after.name` read back, index `-1 -> 0`) and
    still reported `applied: null` because only bool/list containers were understood. That is
    not cosmetic: `save_fx_preset` reads this field, so a successful save would have been
    rolled back as unconfirmed.
    """
    controller, module = preset_controller()
    reads = measured_reads(preset="BassHouse")
    reads["TrackFX_SetPreset"] = lambda *a: 1
    fake_host(module, monkeypatch, **reads)
    answer = controller.set_fx_preset(0, 0, name="BassHouse")
    assert answer["applied"] is True and answer["verified"] is True, answer
    assert answer["call_layout"] == "int", answer

    # the bare zero is a refusal, and nothing became current: reported as the failure it is
    refused = measured_reads()
    refused["TrackFX_SetPreset"] = lambda *a: 0
    fake_host(module, monkeypatch, **refused)
    answer = controller.set_fx_preset(0, 0, name="BassHouse")
    assert answer["applied"] is False and answer["status"] == "error", answer
    assert answer["verified"] is False and answer["after"]["name"] is None, answer


def test_navigation_reports_the_move_it_measured(monkeypatch):
    """`TrackFX_NavigatePresets` answers the bare retval too; before/after come from the reads."""
    controller, module = preset_controller()
    indexes = iter([[1, POINTER, 0, 4], [2, POINTER, 0, 4]])
    names = iter(["pad", "DirtyBassKongo"])
    fake_host(
        module,
        monkeypatch,
        TrackFX_GetPresetIndex=lambda *a: next(indexes),
        TrackFX_GetPreset=lambda *a: [1, POINTER, 0, next(names), 1024],
        TrackFX_NavigatePresets=lambda *a: 1,
    )
    answer = controller.navigate_fx_presets(0, 0, step=1)
    assert answer["moved"] is True and answer["changed"] is True, answer
    assert answer["before"] == {"name": "pad", "index": 1}, answer
    assert answer["after"] == {"name": "DirtyBassKongo", "index": 2, "count": 4}, answer
    assert answer["call_layout"] == "int", answer


# ---------------------------------------------------------------------------
# The end-to-end read the live call performs
# ---------------------------------------------------------------------------


def test_get_fx_presets_reports_the_host_given_numbers_and_the_file(tmp_path, monkeypatch):
    """The live call's own scenario: host says 4, the file says 4, the name buffer is empty."""
    from utils.reaper_presets import append_preset

    controller, module = preset_controller()
    path = tmp_path / "vst3-Vital.ini"
    built = append_preset("[General]\nNbPresets=0\n", "BassHouse", b"x" * 60)
    path.write_text(built["text"], encoding="utf-8", newline="")
    fake_host(module, monkeypatch, **measured_reads(path=str(path)))
    answer = controller.get_fx_presets(0, 0)
    assert answer["host_preset_count"] == 4, answer
    assert answer["file_preset_count"] == 1, answer
    assert answer["user_preset_file"] == str(path), answer
    assert answer["current"] is None, answer
    assert answer["preset_index"] == -1, answer
    assert "disagreement" in answer, "4 in the host vs 1 in the file is a real disagreement"
    assert POINTER not in json.dumps(answer), "the pointer echo must appear nowhere in the answer"
    assert answer["read_shapes"]["current"].startswith("with_buffer:list[int,echo,int,str,int]"), answer
