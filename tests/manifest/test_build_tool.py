"""The build tool is registered, and it builds a real bundle with no host at all.

WHY A FAKE `mcp` OBJECT: the tool body is a closure created by FastMCP's decorator, and what has to
be proved here is (a) that a tool with that name is registered next to the live fill and (b) that
its BODY, called directly, is pure - no controller, no project, no reapy. The fake records the
callables the decorator is given, so the body can be invoked with the real bundle.

A bundle is required for the body test; without one the test SKIPS with the reason, because a
"passed" that never built anything would be worse than no test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

BUNDLE = Path(r"C:\REAPER\work\takes\dnb_v1\aligned_v4")


class FakeMCP:
    """Records what `setup_manifest_tools` registers."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str):
        def register(function):
            self.tools[name] = function
            return function

        return register


class UnusableController:
    """Any attribute access fails: the build path must not need a controller."""

    def __getattr__(self, name: str):
        raise AssertionError("the build tool touched the controller (%s)" % name)


def _registered() -> dict:
    from reaper_reapy_mcp.manifest import setup_manifest_tools

    mcp = FakeMCP()
    setup_manifest_tools(mcp, UnusableController())
    return mcp.tools


def test_both_tools_are_registered():
    tools = _registered()
    assert set(tools) == {"build_project_file", "apply_audiocpp_manifest"}, sorted(tools)


def test_the_build_tool_needs_no_controller_and_no_project(tmp_path: Path):
    if not (BUNDLE / "manifest.json").is_file():
        pytest.skip("the aligned_v4 bundle is not on this machine: %s" % BUNDLE)
    build = _registered()["build_project_file"]
    target = tmp_path / "built.rpp"
    answer = build(
        None,  # ctx is unused by the body
        bundle_dir=str(BUNDLE),
        out_path=str(target),
        title="dnb-neurofunk-174",
        open_after=False,
    )
    assert answer["ok"] is True, answer
    assert answer["project_touched"] is False
    assert target.is_file()
    assert answer["path"] == str(target)
    assert answer["bytes"] == target.stat().st_size

    counts = answer["counts"]
    assert counts["tracks"] == 14
    assert counts["items"] == 57
    assert counts["midi_items"] == 51
    assert counts["audio_items"] == 6
    assert counts["regions"] == 8
    assert counts["markers"] == 1
    assert counts["stretch_markers"] == 145 * 6
    assert counts["midi_events"] == 4405 - 51  # the closing CC123 per source is written, not modelled
    assert answer["bpm"] == pytest.approx(174.6873, abs=1e-9)
    assert answer["meter"] == [4, 4]

    # The whole point of F4: seconds, not minutes. The live fill of this same bundle was measured
    # at 989.2 s; a guard at 30 s still catches "it started filling" without being flaky.
    assert answer["seconds"] < 30.0, answer["seconds"]


def test_a_missing_bundle_is_a_named_refusal(tmp_path: Path):
    build = _registered()["build_project_file"]
    answer = build(None, bundle_dir=str(tmp_path / "nope"), open_after=False)
    assert answer["ok"] is False
    assert "manifest" in json_text(answer).lower()


def json_text(answer: dict) -> str:
    import json

    return json.dumps(answer, ensure_ascii=False)
