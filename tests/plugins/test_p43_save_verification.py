"""P43, third half - what "the state came back" can mean, MEASURED on the owner's own BassHouse.

The save path's first verification demanded byte-identity between the saved blob and the state the
plugin shows after applying it. Live measurement (2026-10-18) showed that is not achievable on this
plugin, and not a defect of the file:

* apply #1 -> 175,761 bytes: the preset is current, but the sampler engine keeps the LIVE runtime
  values (`sample_on` 1.0 vs stored 0.0, `sample_level` 0.8500000238418579 vs 0.7071067690849304,
  `sample_loop` 0.0 vs 1.0, `sample_destination` 4.0 vs 3.0 - the owner's session values);
* apply #2 -> 175,747 bytes = the saved core's own length; the sampler values are the stored ones;
  the single remaining difference is `settings.sample.samples`, the base64 PCM of the preset's own
  sample ("White Noise", 44,100 frames): same length 117,600 characters, first difference at
  character 11,221 (~4 int16 samples of shift);
* apply #3 -> byte-identical to apply #2 (sha256 8f6a920e62fab826).

So the verified chain is: the host re-read the file (count grew), the name became current
(selector), applying the saved preset twice reaches a FIXED POINT, and that point is the same
LENGTH as the bytes that were saved. The byte difference is REPORTED, never asserted away.

Mock host and a real temp file: no REAPER, no connection.
"""

from __future__ import annotations

import base64
import importlib
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CLASS_HEX = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def block_data(core: bytes) -> str:
    """A track chunk carrying ONE plugin block whose item A + item B is `core`."""
    item_a = bytearray(44)
    item_b = core[44:]
    item_a[0x20:0x24] = len(item_b).to_bytes(4, "little")
    head = f'<VST "Vital" Vital.vst3 0 "" {{{CLASS_HEX}}} ""'
    a64 = base64.b64encode(bytes(item_a)).decode("ascii")
    b64 = base64.b64encode(item_b).decode("ascii")
    return f'<TRACK\nNAME "t"\n{head}\n{a64}\n{b64}\n>\n'


def state_core(size: int = 400, *, seed: int = 1) -> bytes:
    """A synthetic FX state: bytes, but a well-formed blob for `split_preset_blob`."""
    item_b = bytes(((index * 7 + seed) % 251 for index in range(size)))
    item_a = bytearray(44)
    item_a[0x20:0x24] = len(item_b).to_bytes(4, "little")
    return bytes(item_a) + item_b


class Scenario:
    """The scripted host: preset file, chunk reads, selector."""

    def __init__(self, tmp_path: Path, *, core: bytes, settled: bytes, settled_again: bytes,
                 before_name="BassHouse", before_count=4, after_count=5, applied=True):
        self.path = tmp_path / "vst3-Vital.ini"
        self.path.write_text("[General]\nNbPresets=1\n[Preset0]\nName=BassHouse\n", encoding="utf-8", newline="")
        self.original = self.path.read_text(encoding="utf-8")
        self.chunks = [block_data(core), block_data(settled), block_data(settled_again)]
        self.reads: list[str] = []
        self.before_name = before_name
        self.before_count = before_count
        self.after_count = after_count
        self.applied = applied
        self.applies: list[str] = []
        self.read_index = 0
        self.index_reads = 0

    def wire(self, monkeypatch, module, controller):
        monkeypatch.setattr(controller, "_read_chunk", self._read_chunk)
        monkeypatch.setattr(controller, "_current_preset", self._current_preset)
        monkeypatch.setattr(controller, "_preset_index", self._preset_index)
        monkeypatch.setattr(controller, "_user_preset_file", lambda ti, fi: (str(self.path), "test"))

        def fake_set(track_index, fx_index, *, name=None, index=None):
            self.applies.append(name if name is not None else index)
            return {"applied": self.applied, "verified": self.applied}

        monkeypatch.setattr(controller, "set_fx_preset", fake_set)

    def _read_chunk(self, track_index: int) -> str:
        self.reads.append("chunk")
        index = min(self.read_index, len(self.chunks) - 1)
        self.read_index += 1
        return self.chunks[index]

    def _current_preset(self, track_index: int, fx_index: int):
        return self.before_name, "test"

    def _preset_index(self, track_index: int, fx_index: int):
        """The real sequence: one read for the plan, one AFTER the file write (the count grew)."""
        self.index_reads += 1
        count = self.before_count if self.index_reads == 1 else self.after_count
        return 0, count, "test"


def scenario_for(tmp_path: Path, **kwargs):
    core = kwargs.pop("core", state_core())
    return Scenario(tmp_path, core=core, **kwargs)


def test_the_settled_state_is_the_criterion_not_the_first_apply(tmp_path, monkeypatch):
    """MEASURED: apply #1 is runtime-polluted (+14 bytes), apply #2 settles, apply #3 is a fixpoint."""
    module = importlib.import_module("controllers.preset_controller")
    core = state_core(400)
    settled = state_core(400, seed=2)
    # the measured shape: the FIRST apply's state is a different LENGTH; the layer must not read it
    scenario = scenario_for(tmp_path, core=core, settled=settled, settled_again=settled)
    controller = module.PresetController.__new__(module.PresetController)
    controller._track = lambda track_index: types.SimpleNamespace(id="(MediaTrack*)0x1")
    scenario.wire(monkeypatch, module, controller)

    answer = controller.save_fx_preset(0, 0, name="MCP Check", dry_run=False)
    assert answer["status"] == "success", answer
    verification = answer["verification"]
    assert verification["stable"] is True, verification
    assert verification["length_matches_saved"] is True, verification
    assert verification["byte_identical_to_saved"] is False, verification
    assert verification["byte_difference"]["read_back_bytes"] == len(settled), verification
    # exactly ONE chunk read for the saved state, then two for the verification pair - the
    # settled state is read after the SECOND application, not after the first
    assert scenario.reads == ["chunk", "chunk", "chunk"], scenario.reads
    # two applications to settle + one fixed-point probe + two to bring the previous preset back
    assert scenario.applies == ["MCP Check", "MCP Check", "MCP Check", "BassHouse", "BassHouse"], scenario.applies
    written = scenario.path.read_text(encoding="utf-8")
    assert "MCP Check" in written and "NbPresets=2" in written, written


def test_a_state_that_never_settles_restores_the_file(tmp_path, monkeypatch):
    module = importlib.import_module("controllers.preset_controller")
    core = state_core(400)
    scenario = scenario_for(
        tmp_path, core=core, settled=state_core(400, seed=2), settled_again=state_core(400, seed=3)
    )
    controller = module.PresetController.__new__(module.PresetController)
    controller._track = lambda track_index: types.SimpleNamespace(id="(MediaTrack*)0x1")
    scenario.wire(monkeypatch, module, controller)

    answer = controller.save_fx_preset(0, 0, name="MCP Check", dry_run=False)
    assert answer["status"] == "error" and answer["restored"] is True, answer
    assert answer["verification"]["stable"] is False, answer
    assert scenario.path.read_text(encoding="utf-8") == scenario.original, "the file is the way back"
    assert scenario.applies == ["MCP Check", "MCP Check", "MCP Check", "BassHouse"], scenario.applies


def test_a_settled_state_of_the_wrong_length_restores_the_file(tmp_path, monkeypatch):
    module = importlib.import_module("controllers.preset_controller")
    scenario = scenario_for(
        tmp_path, core=state_core(400), settled=state_core(600), settled_again=state_core(600)
    )
    controller = module.PresetController.__new__(module.PresetController)
    controller._track = lambda track_index: types.SimpleNamespace(id="(MediaTrack*)0x1")
    scenario.wire(monkeypatch, module, controller)

    answer = controller.save_fx_preset(0, 0, name="MCP Check", dry_run=False)
    assert answer["status"] == "error" and answer["restored"] is True, answer
    assert answer["verification"]["stable"] is True, answer
    assert answer["verification"]["length_matches"] is False, answer
    assert "same length as saved False" in answer["message"], answer["message"]


def test_a_refused_apply_never_reads_the_state_a_second_time(tmp_path, monkeypatch):
    module = importlib.import_module("controllers.preset_controller")
    scenario = scenario_for(
        tmp_path, core=state_core(400), settled=state_core(400), settled_again=state_core(400), applied=False
    )
    controller = module.PresetController.__new__(module.PresetController)
    controller._track = lambda track_index: types.SimpleNamespace(id="(MediaTrack*)0x1")
    scenario.wire(monkeypatch, module, controller)

    answer = controller.save_fx_preset(0, 0, name="MCP Check", dry_run=False)
    assert answer["status"] == "error" and answer["restored"] is True, answer
    assert answer["verification"]["applied"] is False, answer
    assert scenario.reads == ["chunk"], "only the save-time read happened"
    assert scenario.path.read_text(encoding="utf-8") == scenario.original


def test_the_dry_run_writes_nothing_and_reports_the_plan(tmp_path, monkeypatch):
    module = importlib.import_module("controllers.preset_controller")
    scenario = scenario_for(
        tmp_path, core=state_core(400), settled=state_core(400), settled_again=state_core(400)
    )
    controller = module.PresetController.__new__(module.PresetController)
    controller._track = lambda track_index: types.SimpleNamespace(id="(MediaTrack*)0x1")
    scenario.wire(monkeypatch, module, controller)

    answer = controller.save_fx_preset(0, 0, name="MCP Check", dry_run=True)
    assert answer["status"] == "success" and answer["dry_run"] is True, answer
    assert answer["would_write"]["nb_presets_before"] == 1, answer
    assert answer["would_write"]["nb_presets_after"] == 2, answer
    assert scenario.path.read_text(encoding="utf-8") == scenario.original
    assert scenario.applies == [] and scenario.reads == ["chunk"]
