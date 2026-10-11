"""P16 regression: a dead transport is re-established; a write is never retried.

**The defect this pins (measured live, 2026-10-11).** The MCP server process held
a ``ReaperHostBridge`` whose reapy socket had been closed by REAPER while idle.
Every call through the registered tools then answered ``NotConnected``
(``ConnectionAbortedError`` WinError 10053) for the rest of the process's life,
because ``_connected`` had been flipped to True on the first connect and nothing
ever looked at it again. Restarting the server was the only way out - which is
the opposite of what a practical AI-driven workflow needs.

**What is asserted here, offline and without a host.**

1. a fresh bridge connects **and handshakes** before claiming to be connected;
2. a read that meets a dead socket re-establishes the transport and is retried
   **exactly once**, and the retry's value is the returned value;
3. a **write** meeting the same loss is *not* retried (its effect is unknown),
   but the connection is invalidated so the next verb recovers - and the
   invalidation is what makes the engine's verification reads reconnect and
   measure the truth;4. a transport loss on a host that stays down raises after **one** recovery
   attempt, keeps both attempts in the error detail, and does not lock the
   bridge out: the next call tries again;
5. a host-side error that is *not* a transport loss (an ``Unreadable``) never
   triggers a reconnect - the retry path is for transport loss only;
6. a REAPER that was **not running when reapy was imported** becomes usable:
   ``reapy.connect()`` alone re-uses the dead client, ``reapy.reconnect()`` is
   the public call that replaces it;
7. the write verbs route through ``call`` and never through ``call_read`` (a
   static companion to 3, so a future refactor cannot quietly enable write
   retries).

The tests inject a fake client through the bridge's own lazy import seam
(``_reapy`` / ``_api``) and assert the injection before exercising a write verb -
the conftest live-write gate exists against *live* hosts, and nothing here can
reach one: the fake is the only client the bridge is given.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pytest

from reaper_reapy_mcp.plugins.errors import NotConnected, Unreadable
from reaper_reapy_mcp.plugins.host import reaper as host_reaper
from reaper_reapy_mcp.plugins.model import PluginInstanceRef

REF = PluginInstanceRef(
    host="reaper",
    location={"track_index": 0, "fx_index": 0},
    provider="fake",
    identity={},
)


class FakeApi:
    """The ``reapy.reascript_api`` surface the exercised verbs actually use."""

    def __init__(self, host: "FakeHost") -> None:
        self._host = host

    # -- reads the verbs need
    def GetNumTracks(self) -> int:
        self._host.touch("GetNumTracks")
        return self._host.n_tracks

    def GetTrack(self, project: int, index: int):
        self._host.touch("GetTrack")
        return f"track-{index}" if index < self._host.n_tracks else None

    def TrackFX_GetCount(self, track) -> int:  # noqa: ANN001 - host handle shape
        self._host.touch("TrackFX_GetCount")
        return 1

    def GetTrackName(self, track, buffer: str, size: int):  # noqa: ANN001
        self._host.touch("GetTrackName")
        return [1, str(track), "Bass", size]

    def TrackFX_GetFXName(self, track, fx_index: int, buffer: str, size: int):  # noqa: ANN001
        self._host.touch("TrackFX_GetFXName")
        return [1, str(track), fx_index, "VST3i: Fake", size]

    def TrackFX_GetParamFromIdent(self, track, fx_index: int, ident: str):  # noqa: ANN001
        self._host.touch("TrackFX_GetParamFromIdent")
        return [self._host.ident_index(ident)]

    def TrackFX_GetParamNormalized(self, track, fx_index: int, index: int) -> float:  # noqa: ANN001
        self._host.read_calls += 1
        self._host.touch("TrackFX_GetParamNormalized")
        return self._host.value

    def TrackFX_SetParamNormalized(self, track, fx_index: int, index: int, value: float):  # noqa: ANN001
        self._host.touch("TrackFX_SetParamNormalized")
        self._host.value = float(value)
        return 1.0


class FakeHost:
    """A reapy-shaped double whose socket can be killed and re-established."""

    def __init__(self, *, n_tracks: int = 2, dead: bool = False, stay_down: bool = False) -> None:
        self.n_tracks = n_tracks
        self.value = 0.25
        self.dead = dead
        self.stay_down = stay_down
        #: which API calls observe the death; empty means "every call does"
        self.dead_on: set[str] = set()
        self.connect_calls = 0
        self.reconnect_calls = 0
        self.read_calls = 0
        self.api = FakeApi(self)

    # -- the reapy module surface the bridge uses
    def connect(self, host: str | None = None) -> None:
        """``reapy.connect`` **re-uses** a registered client (measured)."""
        self.connect_calls += 1

    def reconnect(self) -> None:
        self.reconnect_calls += 1
        if not self.stay_down:
            self.dead = False  # a fresh socket

    @staticmethod
    def inside_reaper():
        return contextlib.nullcontext()

    # -- the double's own knobs
    def touch(self, api_call: str) -> None:
        if self.dead and (not self.dead_on or api_call in self.dead_on):
            # a socket that closed once is gone for every later call as well
            self.dead_on = set()
            raise ConnectionAbortedError("[WinError 10053] the socket was closed by the host")

    def ident_index(self, ident: str) -> int:
        return 0


def make_bridge(host: FakeHost) -> host_reaper.ReaperHostBridge:
    bridge = host_reaper.ReaperHostBridge()
    bridge._reapy = lambda: host  # type: ignore[method-assign] - the injection seam
    bridge._api = lambda: host.api  # type: ignore[method-assign]
    return bridge


def test_first_use_connects_handshakes_and_reads():
    host = FakeHost()
    bridge = make_bridge(host)

    assert bridge.read_params(REF, [0]) == [0.25]
    assert host.connect_calls == 1, "the first use establishes the client"
    assert host.reconnect_calls == 0, "a healthy transport is not torn down"
    assert bridge.recoveries == [], "a healthy run records no recovery"


def test_a_dead_socket_is_re_established_and_the_read_retried_once():
    host = FakeHost()
    bridge = make_bridge(host)
    assert bridge.read_params(REF, [0]) == [0.25]  # healthy first use

    host.dead = True  # the host closes the socket while idle
    host.dead_on = {"TrackFX_GetParamNormalized"}  # the read itself meets the loss
    assert bridge.read_params(REF, [0]) == [0.25]

    assert host.reconnect_calls == 1, "the transport is re-established once"
    assert host.read_calls == 3, "one initial read, one failed attempt, one retry"
    assert [row["label"] for row in bridge.recoveries] == ["read_params"]
    assert "ConnectionAbortedError" in (bridge.recoveries[0]["transport"] or "")


@pytest.mark.parametrize("label", ["write_params", "write_state_slice"])
def test_a_write_like_call_is_attempted_exactly_once_and_invalidates(label):
    """The two write verbs share this chokepoint (proved statically below).

    A write must **not** be retried: its effect is unknown once the transport
    fails. What must happen instead is that the connection is invalidated, so the
    engine's verification reads reconnect and measure what actually happened -
    the passed test's next read recovering is that behaviour, observed.
    """
    host = FakeHost()
    bridge = make_bridge(host)
    assert bridge.read_params(REF, [0]) == [0.25]

    attempts = {"n": 0}

    def _write_like() -> None:
        attempts["n"] += 1
        host.touch("TrackFX_SetParamNormalized")

    host.dead = True  # the host closes the socket while idle
    with pytest.raises(NotConnected):
        bridge.call(_write_like, timeout=bridge.timeout, label=label)

    assert attempts["n"] == 1, "one attempt, never a retry"
    assert host.reconnect_calls == 0, "no reconnect is spent inside the write"
    assert bridge._connected is False, "the dead transport is invalidated"

    # the next verb recovers, so the engine's verification can measure the truth
    assert bridge.read_params(REF, [0]) == [0.25]
    assert host.reconnect_calls == 1


def test_a_host_side_error_never_triggers_a_reconnect():
    host = FakeHost()
    bridge = make_bridge(host)
    assert bridge.read_params(REF, [0]) == [0.25]

    def _boom(*args, **kwargs):
        raise RuntimeError("the host rejected the request")

    host.api.TrackFX_GetParamNormalized = _boom  # type: ignore[method-assign]
    with pytest.raises(Unreadable):
        bridge.read_params(REF, [0])

    assert host.reconnect_calls == 0, "only a transport loss justifies reconnecting"
    assert bridge._connected is True, "a typed host error leaves the transport alone"
    assert bridge.recoveries == []


def test_a_host_that_stays_down_is_attempted_once_per_call_and_says_why():
    host = FakeHost()
    bridge = make_bridge(host)
    assert bridge.read_params(REF, [0]) == [0.25]

    host.dead = True
    host.stay_down = True
    with pytest.raises(NotConnected) as first:
        bridge.read_params(REF, [0])

    assert host.reconnect_calls == 1, "one recovery attempt per call"
    assert bridge._connected is False
    assert "first_attempt" in first.value.detail, first.value.detail

    with pytest.raises(NotConnected):
        bridge.read_params(REF, [0])
    assert host.reconnect_calls == 2, "a failure does not lock the bridge out"


def test_a_reaper_that_was_not_running_at_import_becomes_usable():
    host = FakeHost(dead=True)  # reapy's import-time connect failed; the client is dead
    bridge = make_bridge(host)

    assert bridge.read_params(REF, [0]) == [0.25]
    assert host.connect_calls == 1, "connect alone re-uses the dead client"
    assert host.reconnect_calls == 1, "the reconnect is what replaces it"


def test_connecting_to_an_absent_api_surface_is_also_recovered():
    """``reapy.reascript_api`` is empty when the dist API never came up.

    Measured behaviour of the library: the module import succeeds, ``connect()``
    is a no-op, and the *first API attribute access* raises ``AttributeError``
    naming ``reascript_api`` - which the bridge classifies as
    ``HostContractUnavailable``. A REAPER that starts later must not require a
    server restart, so this failure is recovered like a transport loss.
    """
    host = FakeHost()

    class AbsentApi:
        def __getattr__(self, name: str):
            raise AttributeError(f"module 'reapy.reascript_api' has no attribute {name!r}")

    bridge = host_reaper.ReaperHostBridge()
    bridge._reapy = lambda: host  # type: ignore[method-assign]

    state = {"api": AbsentApi()}

    def _api():
        return state["api"]

    bridge._api = _api  # type: ignore[method-assign]

    def _reconnect_then_serve() -> None:
        host.reconnect_calls += 1
        state["api"] = host.api

    host.reconnect = _reconnect_then_serve  # type: ignore[method-assign]
    assert not isinstance(state["api"], FakeApi)
    assert bridge.read_params(REF, [0]) == [0.25]
    assert host.reconnect_calls == 1
    assert isinstance(state["api"], FakeApi), "the recovered surface is the live one"


def test_the_write_verbs_never_route_through_the_retrying_read_path():
    """Static companion to the behavioural test: writes call `call`, not `call_read`."""
    tree = ast.parse(Path(host_reaper.__file__).read_text(encoding="utf-8"))
    calls: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "ReaperHostBridge":
            continue
        for func in [item for item in node.body if isinstance(item, ast.FunctionDef)]:
            names = {
                inner.func.attr
                for inner in ast.walk(func)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
            }
            calls[func.name] = names

    for verb in ("write_params", "write_state_slice"):
        assert "call" in calls[verb], f"{verb} must use the single-attempt transport"
        assert "call_read" not in calls[verb], (
            f"{verb} must not retry: a failed write has an unknown effect"
        )
    for verb in ("list_tracks", "fx_facts", "param_idents", "param_names", "read_params", "read_state_chunk"):
        assert "call_read" in calls[verb], f"{verb} is a read and recovers its transport"
