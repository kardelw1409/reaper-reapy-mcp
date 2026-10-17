"""P38: the run log - visibility for a long call, and a bounded directory (2026-10-17).

Owner's ask, verbatim: a tool call that hangs for minutes needs logs, and the log files
should be cleaned automatically after ~24 hours when the server starts.

What is pinned here:

* a line is self-describing JSON (a reader needs no code from this repo to parse it);
* the sweep removes **only our own** files past the window - a broader rule would be a
  claim about files this layer did not create;
* a long chain leaves one line per unit (that is the "is it stuck?" answer), with
  durations and the unit count;
* the log never copies argument **content** (a tool argument can carry a whole document);
* nothing in the log path can fail a call - not a missing directory, not a read-only one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
PLUGINS_SRC = Path(__file__).resolve().parents[2] / "src"
if str(PLUGINS_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGINS_SRC))

import fixtures as F  # noqa: E402
import mock_host as MH  # noqa: E402
from mock_provider import MockProvider  # noqa: E402

from reaper_reapy_mcp.plugins import logs as LOGS  # noqa: E402
from reaper_reapy_mcp.plugins.model import PluginInstanceRef  # noqa: E402
from reaper_reapy_mcp.plugins.resolve import TargetTokenRegistry  # noqa: E402
from reaper_reapy_mcp.plugins.transaction import Transaction  # noqa: E402

MODE_REQUEST = {"state": {"settings.mode": "mono"}, "routes_remove": [{"slot": 1}, {"slot": 2}]}


def lines() -> list[dict]:
    """Every event written to the pinned log directory (conftest points it at tmp_path)."""
    directory = LOGS.log_directory()
    if not directory.is_dir():
        return []
    events: list[dict] = []
    for path in sorted(directory.glob(f"{LOGS.LOG_FILE_PREFIX}*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# The line and the window
# ---------------------------------------------------------------------------


def test_a_line_is_self_describing_json_with_a_utc_stamp():
    assert LOGS.event("hello", answer=42) is not None
    written = lines()
    assert len(written) == 1
    assert written[0]["event"] == "hello"
    assert written[0]["answer"] == 42
    assert written[0]["utc"].endswith("Z")


def test_none_fields_are_left_out_rather_than_written_as_null():
    LOGS.event("hello", present=1, absent=None)
    written = lines()
    assert "absent" not in written[0], "a null would read as a value that was measured"


def test_the_file_is_one_per_utc_day(tmp_path: Path):
    from datetime import datetime, timezone

    day = datetime(2026, 10, 17, tzinfo=timezone.utc)
    other = datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert LOGS.log_file(directory=tmp_path, now=day).name == "mcp-2026-10-17.log"
    assert LOGS.log_file(directory=tmp_path, now=other).name == "mcp-2026-10-18.log"


def test_the_sweep_removes_our_old_files_and_touches_nothing_else(tmp_path: Path):
    old = tmp_path / "mcp-2020-01-01.log"
    fresh = tmp_path / "mcp-2999-01-01.log"
    foreign = tmp_path / "somebody-elses.log"
    other_file = tmp_path / "notes.txt"
    for path in (old, fresh, foreign, other_file):
        path.write_text("{}\n", encoding="utf-8")
    ancient = time.time() - 48 * 3600
    for path in (old, foreign, other_file):
        os.utime(path, (ancient, ancient))

    result = LOGS.sweep(directory=tmp_path, retention_hours_value=24.0, env={})

    assert result["removed"] == ["mcp-2020-01-01.log"]
    assert result["kept"] == 1
    assert not old.exists()
    assert fresh.exists()
    assert foreign.exists(), "a file this layer did not create is not ours to delete"
    assert other_file.exists()


def test_a_missing_directory_is_not_an_error(tmp_path: Path):
    result = LOGS.sweep(directory=tmp_path / "absent", env={})
    assert result["removed"] == [] and result["failed"] == []


def test_the_retention_window_is_configurable_and_a_bad_value_falls_back(tmp_path: Path):
    assert LOGS.retention_hours({"REAPER_MCP_LOG_RETENTION_HOURS": "72"}) == 72.0
    assert LOGS.retention_hours({"REAPER_MCP_LOG_RETENTION_HOURS": "nonsense"}) == (
        LOGS.DEFAULT_RETENTION_HOURS
    )
    assert LOGS.retention_hours({"REAPER_MCP_LOG_RETENTION_HOURS": "0"}) == (
        LOGS.DEFAULT_RETENTION_HOURS
    )
    assert LOGS.DEFAULT_RETENTION_HOURS == 24.0
    # the window is what decides: a one-hour window removes what a 24-hour one keeps
    old = tmp_path / "mcp-2020-01-01.log"
    old.write_text("{}\n", encoding="utf-8")
    ancient = time.time() - 3 * 3600
    os.utime(old, (ancient, ancient))
    assert LOGS.sweep(directory=tmp_path, retention_hours_value=24.0, env={})["removed"] == []
    assert LOGS.sweep(directory=tmp_path, retention_hours_value=1.0, env={})["removed"] == [
        "mcp-2020-01-01.log"
    ]


def test_a_log_directory_that_cannot_be_written_never_fails_a_call(monkeypatch, tmp_path: Path):
    """A log is a diagnostic, not a contract: a tool call must survive its absence."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(LOGS.LOG_DIR_ENV, str(blocker / "logs"))
    assert LOGS.event("hello", answer=1) is None  # silently dropped, not raised
    assert LOGS.sweep(env={})["failed"] or not (blocker / "logs").exists()


# ---------------------------------------------------------------------------
# The chain: one line per unit, and the tool line around it
# ---------------------------------------------------------------------------


def build_host() -> MH.MockHostBridge:
    foreign = MH.MockFxSlot(
        name=F.FOREIGN_NAME,
        module=F.FOREIGN_MODULE,
        class_id=F.FOREIGN_CLASS,
        block=F.foreign_block_text(),
        params={"0:9000": 0.1},
    )
    return MH.MockHostBridge(tracks=[MH.MockTrack(0, "Track 1", [foreign, MH.MockFxSlot()])])


def make_transaction(host: MH.MockHostBridge, request: dict):
    provider = MockProvider()
    facts = host.fx_facts(0, 1)
    registry = TargetTokenRegistry(session_context={"project": "test"})
    token = registry.issue(facts=facts)
    ref = PluginInstanceRef(
        host="reaper",
        location={"track_index": 0, "fx_index": 1},
        provider="demo",
        identity=provider.identify(facts),
    )
    return Transaction(
        provider=provider,
        codec=provider.codec(),
        bridge=host,
        profile=provider.build_profile(provider.identify(facts)),
        ref=ref,
        request=request,
        token=token,
        registry=registry,
        dry_run=False,
        label="test:chain",
    )


def test_a_long_chain_leaves_one_line_per_unit_with_its_duration():
    host = build_host()
    transaction = make_transaction(host, MODE_REQUEST)
    result = transaction.run()
    assert result.ok, result.error
    units = host.count("write_state_slice")
    assert units == 3

    written = lines()
    unit_lines = [row for row in written if row["event"] == "unit"]
    assert len(unit_lines) == units, "one line per unit is the 'is it stuck?' answer"
    assert [row["index"] for row in unit_lines] == [1, 2, 3]
    assert all(row["of"] == units for row in unit_lines)
    assert all(isinstance(row["seconds"], float) for row in unit_lines)
    start = [row for row in written if row["event"] == "transaction.start"]
    end = [row for row in written if row["event"] == "transaction.end"]
    assert start and start[0]["units"] == units
    assert end and end[0]["ok"] is True and end[0]["seconds"] >= 0


def test_a_refused_transaction_says_so_in_the_log():
    host = build_host()
    transaction = make_transaction(host, {"state": {"settings.mode": "mono"}})
    result = transaction.run()
    assert not result.ok
    errors = [row for row in lines() if row["event"] == "transaction.error"]
    assert errors and errors[0]["code"], "the code is what a reader greps for"


def test_a_tool_call_leaves_one_line_with_its_duration_and_no_content():
    """The registered tool's own line: shape, not the user's material."""
    from mcp.server.fastmcp import FastMCP

    from reaper_reapy_mcp.plugins import api as A
    from reaper_reapy_mcp.plugins import tools as T

    mcp = FastMCP("probe")
    T.setup_plugin_tools(mcp, config=A.PluginConfig(provider_ids=(), env={}))
    secret = "Z" * 400
    blocks = asyncio.run(
        mcp.call_tool("plugin_state", {"instance": {"track_index": 0}, "include": [secret]})
    )
    text = next(block.text for block in blocks if getattr(block, "text", None))
    assert json.loads(text)["schema"] == 2

    written = [row for row in lines() if row["event"] == "tool" and row["tool"] == "plugin_state"]
    assert len(written) == 1
    row = written[0]
    assert isinstance(row["seconds"], float)
    assert row["args"]["instance"] == "<object 1 keys: track_index>"
    assert secret not in json.dumps(row), "a log is not a place to copy the caller's material"
    assert "Z" * 10 not in json.dumps(row)
