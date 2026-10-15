"""`save_project`: the guards are the feature, so they are what is tested.

An unsaved project was lost to a reboot in this workspace (it had to be rebuilt from the bundle), and
the reason it stayed unsaved is that nothing in the toolchain ever wrote it: the fill tool can save
(`save_as`) but the pipeline never passed it, and there was no way to ask for a save on its own.

Two things about this tool are easy to get wrong and impossible to notice, so both are pinned here:

  * firing a save with NO name raises REAPER's modal Save-As dialog, and a modal blocks every reapy
    call behind it - a caller would hang instead of getting an answer. The project's current file name
    cannot be read through reapy either (`GetProjectName` and `EnumProjects` return the buffer they
    were given - probed 2026-10-15), so the tool cannot decide on its own whether saving in place is
    safe. Hence: a path is required, and the refusal names the reason.
  * `Main_SaveProjectEx` can return without producing anything (the manifest fill warns about exactly
    that), so the write is verified by re-stating the path - and a file whose mtime did NOT move is
    reported as `rewritten: false` rather than presented as a fresh restore point.

Hermetic: REAPER is not contacted, `RPR`/`reapy` are replaced. Run with the interpreter the REAPER
server itself uses (`py -3.11`), because that is the environment the tool will live in.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import controllers.project_controller as project_module  # noqa: E402
from controllers.project_controller import ProjectController  # noqa: E402


class _FakeRPR:
    """Records the save calls and, optionally, behaves like REAPER: writes the file or nothing."""

    def __init__(self, *, produce: bool = True, keep_mtime: bool = False) -> None:
        self.produce = produce
        self.keep_mtime = keep_mtime
        self.calls: list[tuple[str, int]] = []

    def Main_SaveProjectEx(self, project_id, path, options):
        self.calls.append((str(path), int(options)))
        if not self.produce:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.keep_mtime and target.is_file():
            # a "save" that leaves the existing file exactly as it was
            stamp = target.stat().st_mtime
            os.utime(target, (stamp, stamp))
            return
        target.write_bytes(b"<REAPER_PROJECT> 0.1\n")
        # a coarse filesystem clock must not turn a real save into a false alarm
        later = time.time() + 0.01
        os.utime(target, (later, later))


class _Project:
    id = "(ReaProject*)0xTEST"


@pytest.fixture()
def controller(monkeypatch):
    def _make(**kwargs):
        fake = _FakeRPR(**kwargs)
        monkeypatch.setattr(project_module, "RPR", fake)
        monkeypatch.setattr(project_module.reapy, "Project", lambda: _Project())
        instance = ProjectController(debug=False)
        return instance, fake
    return _make


def test_without_a_path_it_refuses_and_never_fires_a_save(controller):
    """A default path here would be a hang, not a convenience: see the module docstring.

    Through MCP the refusal happens EARLIER - the schema marks `path` required, so the transport
    answers `Field required` before the body runs (measured live). This is the same guard for a
    direct call on the controller, which is why it is tested at this level too.
    """
    saver, fake = controller()

    answer = saver.save_project()

    assert answer["status"] == "error", answer
    assert fake.calls == [], "a save must not be fired without a name"
    assert any("modal" in hint for hint in answer["hints"]), answer


def test_a_wrong_extension_is_refused_before_anything_is_written(tmp_path, controller):
    saver, fake = controller()

    answer = saver.save_project(str(tmp_path / "project.rpp.bak"))

    assert answer["status"] == "error", answer
    assert fake.calls == [], answer


def test_a_path_without_an_extension_is_saved_as_rpp(tmp_path, controller):
    """REAPER writes .rpp; a caller who typed no extension gets the file it wanted, not a miss."""
    saver, fake = controller()

    answer = saver.save_project(str(tmp_path / "aligned"))

    assert answer["status"] == "success", answer
    assert answer["saved_as"].endswith(".rpp") and Path(answer["saved_as"]).is_file(), answer
    assert len(fake.calls) == 1 and fake.calls[0][1] == 0, fake.calls


def test_a_save_that_produces_nothing_is_an_error_that_names_the_path(tmp_path, controller):
    """`Main_SaveProjectEx` returning without a file is a real outcome, and must not read as success."""
    saver, _fake = controller(produce=False)
    target = tmp_path / "project" / "aligned.rpp"

    answer = saver.save_project(str(target))

    assert answer["status"] == "error", answer
    assert answer["requested_path"] == str(target), answer
    assert answer["hints"], answer


def test_the_parent_directory_is_created(tmp_path, controller):
    saver, _fake = controller()
    target = tmp_path / "bundle" / "project" / "aligned.rpp"

    answer = saver.save_project(str(target))

    assert answer["status"] == "success" and Path(answer["saved_as"]).is_file(), answer


def test_a_fresh_write_is_reported_with_its_size_and_as_rewritten(tmp_path, controller):
    saver, _fake = controller()
    target = tmp_path / "aligned.rpp"

    answer = saver.save_project(str(target))

    assert answer["status"] == "success", answer
    assert answer["bytes"] == Path(target).stat().st_size > 0, answer
    assert answer["rewritten"] is True, answer
    assert "warning" not in answer, answer


def test_an_unchanged_mtime_is_not_presented_as_a_fresh_restore_point(tmp_path, controller):
    """The caller asked for a way back; a file that was already there is a way back to somewhere OLDER."""
    target = tmp_path / "aligned.rpp"
    target.write_bytes(b"<REAPER_PROJECT> an older save\n")
    saver, _fake = controller(keep_mtime=True)

    answer = saver.save_project(str(target))

    assert answer["status"] == "success", answer
    assert answer["rewritten"] is False, answer
    assert "mtime" in answer["warning"], answer


def test_the_tool_is_registered_with_the_name_the_pipeline_uses():
    """A guard against the class of defect this workspace has already paid for: a tool that exists in
    the controller while the transport never exposes it (or exposes it under another name)."""
    import asyncio

    from mcp.server.fastmcp import FastMCP

    import mcp_tools

    mcp = FastMCP("save-project-test")
    mcp_tools.setup_mcp_tools(mcp, object())
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert "save_project" in tools, sorted(tools)
    schema = tools["save_project"].inputSchema
    # `path` is REQUIRED: a default here would fire the modal Save-As (see the module docstring)
    assert schema["required"] == ["path"], schema
    assert "ctx" not in schema["properties"], schema
