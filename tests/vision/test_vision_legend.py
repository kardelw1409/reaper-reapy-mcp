"""The orientation legend (`how_to_read`): is it TRUE, and is it COMPLETE?

An agent that has never seen REAPER gets this text on every `vision_state` call, so
the failure to guard against is a legend that drifts: it names a field the answer
does not carry, a tool that is not registered, or a bar conversion that does not
match the project. Each of those would send a model looking for something that does
not exist, and it would look perfectly plausible while doing so.

So these tests check the legend AGAINST the thing it describes:

  * every `vision_*` name it mentions is a registered tool;
  * every answer field it references (`bar_seconds`, `beat_seconds`, `media`,
    `media_settings`, `state_change_delta`, `view`, `h_zoom_px_per_s`) is really in
    the `vision_state` answer;
  * the bar/beat arithmetic it teaches matches the metre the host reports (2/4 at
    150.121 BPM: a bar is 0.799 s, NOT the 1.6 s a 4/4 assumption gives).
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from reaper_reapy_mcp.vision import tools
from reaper_reapy_mcp.vision.legend import HOW_TO_READ

SECTIONS = ("units", "window", "items", "grid", "cursors", "workflow", "trust")


def test_legend_has_every_section_and_no_empty_text():
    assert set(HOW_TO_READ) == set(SECTIONS)
    for section, lines in HOW_TO_READ.items():
        assert isinstance(lines, list) and lines, section
        for line in lines:
            assert isinstance(line, str) and len(line) > 20, (section, line)
            # A legend is read by a model, not rendered: no placeholders.
            assert "TODO" not in line and "XXX" not in line, (section, line)


def test_legend_names_only_registered_tools():
    mentioned = set()
    for lines in HOW_TO_READ.values():
        for line in lines:
            mentioned.update(re.findall(r"vision_[a-z]+", line))
    assert mentioned, "the legend has to point at the tools by name"
    unknown = mentioned - set(tools.TOOL_NAMES)
    assert not unknown, f"the legend names tools that do not exist: {sorted(unknown)}"


def test_legend_references_only_fields_the_state_answer_carries(server):
    """Every field named in the legend must be in the answer that carries it."""
    mcp, _frames = server
    content = asyncio.run(mcp.call_tool("vision_state", {}))
    if isinstance(content, tuple):
        content = content[0]
    texts = [block for block in content if getattr(block, "type", None) == "text"]
    payload = json.loads(texts[-1].text)

    legend = "\n".join(line for lines in HOW_TO_READ.values() for line in lines)
    for field in ("bar_seconds", "beat_seconds", "h_zoom_px_per_s", "state_change_delta",
                  "media_settings", "media", "view"):
        if field in legend:
            scope = {**payload, **payload.get("project", {})}
            assert field in scope, (f"the legend mentions `{field}` but the answer "
                                    f"does not carry it (keys: {sorted(scope)})")


def test_state_teaches_bar_and_beat_seconds_from_the_real_metre(server):
    """2/4 at 150.121 BPM: a bar is 0.799 s. A 4/4 assumption would say 1.599."""
    mcp, _frames = server
    content = asyncio.run(mcp.call_tool("vision_state", {}))
    if isinstance(content, tuple):
        content = content[0]
    texts = [block for block in content if getattr(block, "type", None) == "text"]
    payload = json.loads(texts[-1].text)

    project = payload["project"]
    assert project["time_signature"]["numerator"] == 2
    assert project["time_signature"]["denominator"] == 4
    assert project["bar_seconds"] == pytest.approx(0.79936, rel=1e-4)
    assert project["beat_seconds"] == pytest.approx(0.39968, rel=1e-4)
    assert project["bar_source"] == "measurement"
    # The legend's own arithmetic has to agree with what it returns.
    assert 16 / project["bar_seconds"] == pytest.approx(20.0, abs=0.1)
    assert "20 bars" in "\n".join(HOW_TO_READ["units"])


# --- the same knowledge through the other two MCP channels ------------------

def test_the_legend_is_also_a_prompt_and_a_resource(server):
    """Three channels on purpose: a tool field, an opt-in prompt, a resource.

    Only the tool field arrives with no user action, which is what makes the vision
    layer self-sufficient on its first call; the prompt and the resource are for a
    client that asks for the whole guide. All three must stay in step, and this is
    where a registration that stopped working would be noticed - `list_prompts`
    quietly returning nothing is invisible from the tool answers alone.
    """
    mcp, _frames = server

    prompts = [p.name for p in asyncio.run(mcp.list_prompts())]
    assert "reaper_vision_guide" in prompts, prompts

    resources = [str(r.uri) for r in asyncio.run(mcp.list_resources())]
    assert "reaper://vision/legend" in resources, resources

    rendered = asyncio.run(mcp.get_prompt("reaper_vision_guide", {}))
    text = rendered.messages[0].content.text
    for section, lines in HOW_TO_READ.items():
        assert section.upper() in text, section
        # The rendering must carry the WORDS, not the Python repr of the dict.
        assert lines[0][:40] in text, section
    assert "{" not in text.split("\n")[0], "the prompt leaked a dict repr"

    read = asyncio.run(mcp.read_resource("reaper://vision/legend"))
    body = read[0].content if isinstance(read, list) else read
    assert "How to read a REAPER arrangement view" in str(body)
    assert "SECONDS of project time" in str(body)


def test_registering_prompt_and_resource_does_not_change_the_tool_list(server):
    """A client sees the same tools as before: no name appeared or disappeared."""
    mcp, _frames = server
    names = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert set(names) == set(tools.TOOL_NAMES), names
