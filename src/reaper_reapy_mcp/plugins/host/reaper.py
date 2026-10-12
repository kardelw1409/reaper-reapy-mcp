"""REAPER `HostBridge` implementation (P3 reads, P5 write transport).

This is the ONLY module inside `plugins/` that may touch the host API (08 §8),
and it does so **lazily**: importing this module performs no host I/O, so the
offline slice/conformance tests run with REAPER closed. Every host call goes
through the single timeout/wedge chokepoint.

**Transport only, no semantics (AUD-004).** The write verbs splice bytes and
call the host setter; they contain no provider predicate, no verification
callback, no tolerance and no plugin vocabulary - Core decodes and verifies
through the provider codec. `test_p5_transport.py` asserts that mechanically,
and the plugin test suite refuses any live write until the P8 gate (a conftest
guard, not an aspiration).

The chunk parser, the slice locator and the splice are pure functions: the
correlation between a state block and the addressed instance must be **unique
or refuse** (AUD-001), and writes replace exactly the located span, leaving
every other byte of the chunk untouched.

**Not live-certified:** the write verbs below are proven against the mock host
and captured chunk strings only. Their behaviour against a real REAPER
instance is measured in P8; until then they are a tested abstraction.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from ..errors import (
    HostContractUnavailable,
    NotConnected,
    PluginError,
    StateConflict,
    TargetSliceAmbiguous,
    Unreadable,
    Wedged,
)
from ..model import (
    FxHostFacts,
    PluginInstanceRef,
    StateWriteUnit,
    TargetedStateSlice,
    TimingProfile,
    TrackView,
    WriteAttempt,
    text_digest,
    utc_now_iso,
)

__all__ = [
    "ReaperHostBridge",
    "parse_blocks",
    "locate_block_span",
    "splice_block_text",
    "DEFAULT_TIMEOUT_S",
]

DEFAULT_TIMEOUT_S = 20.0

_CHUNK_CAPACITY = 20_000_000
_STRING_CAPACITY = 512
_CLASS_RE = re.compile(r"\b([0-9A-Fa-f]{32})\b")
_QUOTED_RE = re.compile(r'"([^"]*)"')
#: The module token after the quoted FX name. REAPER quotes a token that
#: contains a space (`"Surge XT.vst3"`), and a bare `(\S+)` capture truncates it
#: at the first space (`"Surge`), so a quoted token is read as a whole (measured
#: live 2026-10-12 on a VST3 synth whose module filename contains a space; a
#: space-free module name is parsed correctly and hid the defect).
_MODULE_RE = re.compile(r'"[^"]*"\s+(?:"([^"]*)"|(\S+))')

#: Metadata channels the batched metadata verb attempts (P26). `sections` is
#: probed separately because it depends on the *host build*, not on the plugin.
_METADATA_CHANNELS: tuple[str, ...] = ("name", "ident", "value", "steps", "formatted", "named")


def field_string(raw: Any, index: int) -> str | None:
    """The string field at a **measured** position of a reapy tuple result.

    reapy wraps the C reascript functions, so a function taking a `char* buf`
    returns ``[retval, <pointer>, <inputs...>, buf, buf_sz]``: the buffer sits
    after the inputs, and the position is different for every signature. The
    positions below were **measured live** (P8, 2026-10-10); the plugin name is
    redacted here because this module may not carry provider identifiers (08 §8),
    and the recorded literal is in the P8 evidence and the status journal:

    ===============================================  ================================================
    call                                             measured result
    ===============================================  ================================================
    ``GetTrackName(track, "", 512)``                 ``[1, "(MediaTrack*)0x…", "Track 1", 512]``
    ``TrackFX_GetFXName(track, 0, "", 4096)``         ``[1, "(MediaTrack*)0x…", 0, "<fx name>", 4096]``
    ``TrackFX_GetParamIdent(track, 0, 0, "", 512)``   ``[1, "(MediaTrack*)0x…", 0, 0, "0:48", 512]``
    ``TrackFX_GetParamName(track, 0, 0, "", 512)``    ``[1, "(MediaTrack*)0x…", 0, 0, "<param name>", 512]``
    ===============================================  ================================================

    Reading the wrong position is invisible offline (a tuple's element 0 is the
    return code, which *is* a truthy string once stringified) and produced a
    measured defect: ``GetTrackName`` was read at index 0, so every track was
    reported as ``"1"``. The recorded shapes are asserted by
    `tests/plugins/test_p8_live_shapes.py`.
    """
    if isinstance(raw, (list, tuple)):
        if len(raw) <= index:
            return None
        value = raw[index]
        return str(value) if isinstance(value, str) and value else None
    return str(raw) if isinstance(raw, str) and raw else None


# ---------------------------------------------------------------------------
# Pure chunk parsing (no host access)
# ---------------------------------------------------------------------------


def parse_blocks(chunk: str) -> list[dict[str, Any]]:
    """Every `<VST ...>` block of the chunk with its span, header and identity.

    A track chunk can contain several VST blocks and other FX types; the span
    is what a targeted read or write may ever address.
    """
    lines = chunk.split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("<VST "):
            end = next(
                (k for k in range(index + 1, len(lines)) if lines[k].rstrip("\r") == ">"),
                None,
            )
            if end is None:
                break
            header = lines[index]
            class_match = _CLASS_RE.search(header)
            quoted = _QUOTED_RE.search(header)
            module_match = _MODULE_RE.search(header)
            module: str | None = None
            if module_match:
                module = (
                    module_match.group(1)
                    if module_match.group(1) is not None
                    else module_match.group(2)
                ) or None  # a quoted-empty module is "no evidence", not the token `""`
            blocks.append(
                {
                    "header": header.rstrip("\r"),
                    "class_hex": class_match.group(1).upper() if class_match else None,
                    "name": quoted.group(1) if quoted else None,
                    "module": module,
                    "line_start": index,
                    "line_end": end,
                    "block_text": "\n".join(line.rstrip("\r") for line in lines[index : end + 1]),
                }
            )
            index = end + 1
            continue
        index += 1
    return blocks


def locate_block_span(
    chunk: str,
    *,
    class_hex: str | None,
    module: str | None,
    ordinal: int | None = None,
    matching_fx_count: int | None = None,
) -> TargetedStateSlice:
    """Locate the exact block span uniquely correlated to the target identity.

    The correlation is decided from the declared identity signals (class id and
    module - the module string is what distinguishes a VST2 build from a VST3
    build of the same plugin) plus the target's **ordinal among the matching FX
    slots** when several instances share the identity. Anything ambiguous or
    inconsistent refuses; nothing is ever chosen by position alone.
    """
    if not class_hex and not module:
        raise TargetSliceAmbiguous(
            "no distinguishing identity evidence (class id/module) to correlate the block",
            detail={"class_hex": class_hex, "module": module},
        )
    blocks = parse_blocks(chunk)
    matching = [
        block
        for block in blocks
        if (class_hex is None or block["class_hex"] == class_hex.upper())
        and (module is None or block["module"] == module)
    ]
    if not matching:
        raise Unreadable(
            "no matching state block found in the chunk",
            detail={"n_vst_blocks": len(blocks), "classes": [b["class_hex"] for b in blocks]},
        )
    if matching_fx_count is not None and matching_fx_count != len(matching):
        raise TargetSliceAmbiguous(
            "the number of matching blocks disagrees with the host's matching FX slots",
            detail={
                "matching_blocks": len(matching),
                "matching_fx_slots": matching_fx_count,
                "classes": [b["class_hex"] for b in matching],
            },
        )
    if len(matching) == 1:
        if ordinal not in (None, 0):
            raise TargetSliceAmbiguous(
                "the host places the target at a later position than the single matching block",
                detail={"ordinal": ordinal, "matching_blocks": 1},
            )
        chosen, chosen_ordinal = matching[0], 0
    else:
        if matching_fx_count is None:
            raise TargetSliceAmbiguous(
                "several blocks match the identity and the host count agreement is missing",
                detail={"matching_blocks": len(matching), "ordinal": ordinal},
            )
        if ordinal is None:
            raise TargetSliceAmbiguous(
                "several blocks match the identity and no ordinal is available",
                detail={"matching_blocks": len(matching)},
            )
        if not 0 <= ordinal < len(matching):
            raise TargetSliceAmbiguous(
                "the ordinal cannot address any matching block",
                detail={"ordinal": ordinal, "matching_blocks": len(matching)},
            )
        chosen, chosen_ordinal = matching[ordinal], ordinal
    return TargetedStateSlice(
        chunk_digest=text_digest(chunk),
        line_start=chosen["line_start"],
        line_end=chosen["line_end"],
        header_line=chosen["header"],
        class_hex=chosen["class_hex"],
        ordinal=chosen_ordinal,
        block_text=chosen["block_text"],
        framing=None,
    )


# ---------------------------------------------------------------------------
# Pure chunk splicing (no host access)
# ---------------------------------------------------------------------------


def _line_ending(raw_line: str) -> str:
    if raw_line.endswith("\r\n"):
        return "\r\n"
    if raw_line.endswith("\n"):
        return "\n"
    return ""


def split_lines_keeping_endings(chunk: str) -> list[str]:
    """Split a chunk on ``"\n"`` **only**, keeping each line's ending.

    The locator (`parse_blocks`, `locate_block_span`) numbers lines in
    ``chunk.split("\n")`` space. `str.splitlines` additionally breaks on
    ``\x0b \x0c \x1c \x1d \x1e \x85 \u2028 \u2029``, so using it here would
    shift every index after such a character and splice the **wrong** lines -
    verified by a regression fixture with a form feed in an earlier line.

    A chunk that **ends** with a newline yields a trailing empty element, and it
    is kept: dropping it - as this helper did until the P8 live measurement -
    made ``"".join(lines)`` one character shorter than the chunk, so every state
    write silently deleted the chunk's final newline. That is a byte **outside**
    the target span, i.e. exactly the property AUD-001 promises to preserve, and
    the live chunk shape (measured 2026-10-10: LF-only, 871 newlines, a
    terminator line after the plugin block, last byte a newline) is where it
    showed up. `test_p5_1h*` pins it.
    """
    parts = chunk.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts else [])


def splice_block_text(chunk: str, slice: TargetedStateSlice, new_block_text: str) -> str:
    """Replace exactly the located span with ``new_block_text``.

    The line ending of the replaced region is preserved (a CRLF chunk stays
    CRLF, and a span that ends at the chunk's last line without an ending gains
    none), and every byte of the chunk outside the span is copied verbatim: a
    foreign or sibling block can never be touched (AUD-001). The span must lie
    inside the chunk it was located from, and a stale slice refuses.
    """
    lines = split_lines_keeping_endings(chunk)
    if not (0 <= slice.line_start <= slice.line_end < len(lines)):
        raise Unreadable(
            "the slice span lies outside the chunk it is being spliced into",
            detail={
                "line_start": slice.line_start,
                "line_end": slice.line_end,
                "chunk_lines": len(lines),
            },
        )
    ending = _line_ending(lines[slice.line_end])
    payload = new_block_text.split("\n")
    replacement = ending.join(payload) + ending
    return "".join(lines[: slice.line_start]) + replacement + "".join(lines[slice.line_end + 1 :])


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class ReaperHostBridge:
    """REAPER transport: the read contract (P3) and the write transport (P5).

    The write verbs are **not live-certified**: P5 proves them against the mock
    host and captured chunks; the live-write gate stands until P8.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        host: str | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.timeout = float(timeout)
        self.host = host
        self._sleeper = sleeper or time.sleep
        self._connected = False
        #: transport-lengthbook: every re-establishment, with the label that
        #: observed the loss. Never silent - a recovery is a fact about the run.
        self.recoveries: list[dict[str, Any]] = []

    # -- connection ---------------------------------------------------------

    def _reapy(self) -> Any:
        """Import the host client lazily (module import must stay I/O-free)."""
        try:
            import reapy  # noqa: PLC0415 - deliberate lazy import (see module docstring)
        except Exception as exc:  # noqa: BLE001
            raise HostContractUnavailable(
                "the host client library is not importable", detail={"error": str(exc)}
            ) from exc
        return reapy

    def _api(self) -> Any:
        try:
            from reapy import reascript_api as R  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise HostContractUnavailable(
                "the host API module is not available", detail={"error": str(exc)}
            ) from exc
        return R

    def _hold(self):
        """A **held** REAPER context for a batched loop (08 §4.3).

        Measured live (P8, 2026-10-10) on the 2858-parameter instance: 200
        ident reads take **6.30 s** unheld and **0.21 s** held, the full
        ident table **2.61 s**; the complete parameter capture is therefore
        ~5 s instead of a multi-minute loop. Without the hold, the transaction's
        snapshot-or-refuse capture of the full vector cannot complete inside any
        sane timeout - which is a *finding about the transport*, not a reason to
        capture less: the capture stays complete (AUD-002) and the cost is paid
        once per transaction.

        The context is entered inside the same call thread the loop runs in, and
        `reapy` balances HOLD/RELEASE itself. A call that times out leaves the
        client holding until its daemon thread exits - the same transport wedge
        caveat the engine already accounts as an unknown write effect.
        """
        return self._reapy().inside_reaper()

    def ensure(self) -> None:
        """Lazy connection that must be **alive**, not merely established once.

        Measured defect this replaces (2026-10-11): the bridge flipped
        ``_connected`` to True on the first successful connect and never looked
        again. A long-lived server process therefore returned ``NotConnected``
        for **every** call after REAPER closed the socket while idle
        (``ConnectionAbortedError`` WinError 10053, observed live through the
        registered tools), until the whole server was restarted - the exact
        opposite of a practical AI workflow. Two things changed:

        * ``reapy.connect()`` alone is not enough to (re-)establish a client:
          reapy keeps a client per host in ``machines.CLIENTS`` and ``connect``
          **re-uses** it, dead socket included. ``reapy.reconnect()`` is the
          public call that drops it and builds a new one - and it is also the
          documented remedy for "REAPER was not running when reapy was
          imported", which previously left this bridge permanently unusable in
          an otherwise healthy session.
        * a connect is now followed by a **handshake** (one cheap read through
          the same timeout chokepoint), so "connected" means a live transport
          answered - not that a function returned.

        A failed attempt is retried **once** through the reconnect; the error
        raised is the second attempt's, with the first recorded in its detail so
        the original condition is not lost.
        """
        if self._connected:
            return
        first: Exception | None = None
        try:
            self._connect_client()
            self._handshake()
        except Exception as exc:  # noqa: BLE001 - classified and re-raised below
            first = exc
            self._connected = False
            try:
                self._reconnect_client()
                self._handshake()
            except Exception as second:  # noqa: BLE001 - classified below
                raise self._connection_error(second, first=first) from second
        self._connected = True

    def _connect_client(self) -> None:
        """Select a client for the configured host (creating one if unknown)."""
        reapy = self._reapy()

        def _connect() -> None:
            if self.host:
                reapy.connect(host=self.host)
            else:
                reapy.connect()

        self.call(_connect, timeout=self.timeout, label="connect")

    def _reconnect_client(self) -> None:
        """Drop the client reapy holds and build a fresh one (public API).

        ``reapy.reconnect()`` deletes ``machines.CLIENTS[host]`` and calls
        ``connect(host)`` again, then reloads ``reapy.reascript_api`` so the
        wrappers route to the new socket; with an explicit host configured the
        equivalent is an explicit reconnect of that host. Nothing private is
        touched: this is the API reapy documents for exactly this situation.
        """
        reapy = self._reapy()

        def _reconnect() -> None:
            reapy.reconnect()
            if self.host:
                reapy.connect(host=self.host)

        self.call(_reconnect, timeout=self.timeout, label="reconnect")

    def _handshake(self) -> None:
        """One cheap read that proves the transport answers right now."""

        def _probe() -> int:
            R = self._api()
            return int(R.GetNumTracks())

        self.call(_probe, timeout=self.timeout, label="handshake")

    @staticmethod
    def _connection_error(exc: Exception, *, first: Exception | None) -> Exception:
        """The typed error a failed connect/reconnect answers with."""
        if isinstance(exc, PluginError):
            if first is not None and first is not exc:
                exc.detail.setdefault(
                    "first_attempt",
                    f"{type(first).__name__}: {first}",
                )
            return exc
        return NotConnected(
            "the host connection could not be established",
            detail={
                "error": f"{type(exc).__name__}: {exc}",
                "first_attempt": None if first is None else f"{type(first).__name__}: {first}",
            },
        )

    def reconnect(self) -> None:
        self._connected = False
        self.ensure()

    def call_read(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None, label: str = "host read") -> Any:
        """A host **read** with transport-loss recovery (never a write).

        A transport loss invalidates the connection and the read is retried
        **exactly once** after a re-establishment - reads are idempotent, so a
        retry cannot change the host. A write deliberately does **not** use this
        path: a failed write has an *unknown* effect and retrying it blindly
        would be the one thing the transaction engine must never do; writes call
        :meth:`call` and leave the recovery to the next verb (the engine's
        verification reads then reconnect transparently and measure the truth).
        """
        try:
            return self.call(fn, *args, timeout=timeout, label=label)
        except NotConnected as exc:
            self._connected = False
            self.ensure()
            self.recoveries.append(
                {
                    "label": label,
                    "of": "read",
                    "observed": f"{type(exc).__name__}: {exc}",
                    "transport": (exc.detail or {}).get("error"),
                    "utc": utc_now_iso(),
                }
            )
            return self.call(fn, *args, timeout=timeout, label=label)

    # -- transport chokepoint ----------------------------------------------

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None, label: str = "host call") -> Any:
        """Run one host call with a hard timeout; classify the failure.

        A hung host must never hang the server: the call runs in a daemon thread
        and a timeout is ``Wedged``. The connection must already be ensured by
        the caller (`ensure()`), so a connection failure here is a transport
        loss, not a startup problem.
        """
        budget = float(timeout if timeout is not None else self.timeout)
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - classified below
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(budget)
        if thread.is_alive():
            raise Wedged(
                f"host call did not return within {budget}s",
                detail={"label": label, "timeout_s": budget},
            )
        if "error" in outcome:
            error = self._classify(outcome["error"], label)
            if isinstance(error, NotConnected) and self._connected:
                # a transport loss on an *established* client: the client is
                # dead from here on, so the next verb must re-establish it
                # (the measured defect: the flag used to stay True forever)
                self._connected = False
            raise error
        return outcome.get("value")

    @staticmethod
    def _classify(exc: BaseException, label: str) -> Exception:
        name = type(exc).__name__
        module = type(exc).__module__ or ""
        if module.startswith("reapy") or name in ("ReapyError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError"):
            return NotConnected(
                f"host transport failure during {label}", detail={"error": f"{name}: {exc}"}
            )
        if isinstance(exc, (AttributeError, TypeError)) and "reascript_api" in str(exc):
            return HostContractUnavailable(
                f"the host API surface is not what the bridge expects during {label}",
                detail={"error": f"{name}: {exc}"},
            )
        if isinstance(exc, (ConnectionError, OSError)):
            return NotConnected(
                f"host transport failure during {label}", detail={"error": f"{name}: {exc}"}
            )
        return Unreadable(f"{label} failed", detail={"error": f"{name}: {exc}"})

    # -- helpers ------------------------------------------------------------

    def _track(self, R: Any, track_index: int) -> Any:
        track = R.GetTrack(0, track_index)
        if not track:
            raise Unreadable("the track does not exist", detail={"track_index": track_index})
        return track

    def _track_name(self, R: Any, track: Any) -> str:
        return field_string(R.GetTrackName(track, "", _STRING_CAPACITY), 2) or ""

    def _fx_name(self, R: Any, track: Any, fx_index: int) -> str:
        return field_string(R.TrackFX_GetFXName(track, fx_index, "", 4096), 3) or ""

    def _ident(self, R: Any, track: Any, fx_index: int, index: int) -> str | None:
        return field_string(
            R.TrackFX_GetParamIdent(track, fx_index, index, "", _STRING_CAPACITY), 4
        )

    def _param_name(self, R: Any, track: Any, fx_index: int, index: int) -> str | None:
        return field_string(
            R.TrackFX_GetParamName(track, fx_index, index, "", _STRING_CAPACITY), 4
        )

    def _header_identity(self, chunk: str, *, name: str, ordinal_of_name: int) -> dict[str, Any]:
        """Class id + module for the addressed slot, from the chunk headers.

        The module string is load-bearing (a VST2 and a VST3 build of one plugin
        report the same class id), and the correlation to the addressed slot is
        the ordinal among blocks the host reports with the same name. Nothing is
        guessed when the correlation is not unique.
        """
        blocks = [block for block in parse_blocks(chunk) if block["name"] == name]
        if ordinal_of_name < 0 or ordinal_of_name >= len(blocks):
            return {"class_hex": None, "module": None}
        block = blocks[ordinal_of_name]
        return {"class_hex": block["class_hex"], "module": block["module"]}

    def _name_ordinal(self, R: Any, track: Any, fx_index: int, name: str) -> tuple[int, int]:
        n_fx = int(R.TrackFX_GetCount(track))
        matches = [index for index in range(n_fx) if self._fx_name(R, track, index) == name]
        if fx_index not in matches:
            return 0, len(matches)
        return matches.index(fx_index), len(matches)

    # -- HostBridge: reads --------------------------------------------------

    def list_tracks(self) -> list[TrackView]:
        self.ensure()
        R = self._api()

        def _read() -> list[TrackView]:
            count = int(R.GetNumTracks())
            views: list[TrackView] = []
            for index in range(count):
                track = R.GetTrack(0, index)
                views.append(
                    TrackView(
                        track_index=index,
                        name=self._track_name(R, track),
                        n_fx=int(R.TrackFX_GetCount(track)),
                    )
                )
            return views

        return self.call_read(_read, timeout=self.timeout, label="list_tracks")

    def fx_facts(self, track_index: int, fx_index: int) -> FxHostFacts:
        self.ensure()
        R = self._api()

        def _read() -> dict[str, Any]:
            track = self._track(R, track_index)
            n_fx = int(R.TrackFX_GetCount(track))
            if fx_index < 0 or fx_index >= n_fx:
                raise Unreadable(
                    "the FX slot does not exist",
                    detail={"track_index": track_index, "fx_index": fx_index, "n_fx": n_fx},
                )
            name = self._fx_name(R, track, fx_index)
            ordinal, matching = self._name_ordinal(R, track, fx_index, name)
            chunk_res = R.GetTrackStateChunk(track, "", _CHUNK_CAPACITY, False)
            chunk = str(chunk_res[2]) if isinstance(chunk_res, (list, tuple)) and len(chunk_res) > 2 else ""
            identity = self._header_identity(chunk, name=name, ordinal_of_name=ordinal)
            guid = None
            try:
                guid = str(R.TrackFX_GetFXGUID(track, fx_index))
            except Exception:  # noqa: BLE001 - session evidence only
                guid = None
            return {
                "fx_name": name,
                "class_id": identity["class_hex"],
                "module": identity["module"],
                "param_count": int(R.TrackFX_GetNumParams(track, fx_index)),
                "track_name": self._track_name(R, track),
                "fx_count": n_fx,
                "matching_fx_count": matching,
                "session_handle": guid,
            }

        data = self.call_read(_read, timeout=self.timeout, label="fx_facts")
        return FxHostFacts(
            track_index=track_index,
            fx_index=fx_index,
            fx_name=data["fx_name"],
            class_id=data["class_id"],
            module=data["module"],
            param_count=data["param_count"],
            version=None,  # the version lives in the state document (provider territory)
            track_name=data["track_name"],
            fx_count=data["fx_count"],
            session_handle=data["session_handle"],
        )

    def param_idents(self, ref: PluginInstanceRef, indices: list[int]) -> list[str | None]:
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)

        def _read() -> list[str | None]:
            with self._hold():
                track = self._track(R, track_index)
                return [self._ident(R, track, fx_index, index) for index in indices]

        return self.call_read(_read, timeout=self.timeout, label="param_idents")

    def param_names(self, ref: PluginInstanceRef, indices: list[int]) -> list[str | None]:
        """Host-reported parameter **names** for indices; ONE batched hop.

        This is the evidence Core uses to verify that a declared binding index
        still addresses the control it was measured on (the live ident is
        opaque and proves nothing about *which* parameter it belongs to).
        """
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)

        def _read() -> list[str | None]:
            with self._hold():
                track = self._track(R, track_index)
                return [self._param_name(R, track, fx_index, index) for index in indices]

        return self.call_read(_read, timeout=self.timeout, label="param_names")

    def read_params(self, ref: PluginInstanceRef, indices: list[int]) -> list[float]:
        """ONE batched host hop for the whole index list (never per control)."""
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)

        def _read() -> list[float]:
            with self._hold():
                track = self._track(R, track_index)
                return [float(R.TrackFX_GetParamNormalized(track, fx_index, index)) for index in indices]

        return self.call_read(_read, timeout=self.timeout, label="read_params")

    def read_state_chunk(self, ref: PluginInstanceRef) -> str:
        self.ensure()
        R = self._api()
        track_index, _fx_index = _location(ref)

        def _read() -> str:
            track = self._track(R, track_index)
            raw = R.GetTrackStateChunk(track, "", _CHUNK_CAPACITY, False)
            return str(raw[2]) if isinstance(raw, (list, tuple)) and len(raw) > 2 else str(raw)

        return self.call_read(_read, timeout=self.timeout, label="read_state_chunk")

    # -- HostBridge: writes (P5; NOT live-certified until P8) ---------------

    def write_params(self, ref: PluginInstanceRef, pairs: list[tuple[str, float]]) -> WriteAttempt:
        """ONE batched, ident-addressed parameter write with read-back.

        The ident is resolved to a parameter index *inside the same hop* the
        setter runs in (08 §4.3): indices go stale between hops, idents are the
        durable binding on VST3 and there is deliberately no client-side index
        cache. The read-back is recorded with the ident, but **no comparison
        happens here** - establishing the effect is Core's job through the
        provider policy (AUD-004), so ``effects_verified`` stays ``None``.
        """
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)

        def _write() -> list[dict[str, Any]]:
            track = self._track(R, track_index)
            rows: list[dict[str, Any]] = []
            with self._hold():
                for ident, value in pairs:
                    index = _ident_index(R, track, fx_index, ident)
                    if index is None:
                        raise Unreadable(
                            "the parameter ident is not present on the addressed instance",
                            detail={"ident": str(ident), "fx_index": fx_index},
                        )
                    returned = R.TrackFX_SetParamNormalized(track, fx_index, index, float(value))
                    rows.append(
                        {
                            "ident": str(ident),
                            "index": index,
                            "sent": float(value),
                            "setter_returned": returned,
                        }
                    )
                for row in rows:
                    row["read_back"] = float(
                        R.TrackFX_GetParamNormalized(track, fx_index, row["index"])
                    )
            return rows

        rows = self.call(_write, timeout=self.timeout, label="write_params")
        return WriteAttempt(
            unit_index=-1,
            attempted_utc=utc_now_iso(),
            returned={"channel": "parameters", "rows": rows},
            effects_verified=None,  # established by Core's verify, never by the setter
        )

    def write_params_by_index(
        self, ref: PluginInstanceRef, pairs: list[tuple[int, str, float]]
    ) -> WriteAttempt:
        """ONE batched, index-addressed parameter write, guarded by a live name read.

        The providerless path has no ident table to bind against: what its
        prepared artifact measured is the **index** and the parameter's name at
        that index. An index is not a durable address (a plugin can reorder its
        parameters), so the guard is a live ``TrackFX_GetParamName`` read at the
        same index, **inside the same held hop** as the setter: a mismatch raises
        before that parameter's setter runs, and no index is ever written on the
        strength of a stale measurement.

        The (index, expected_name) pair is positional: ``pairs`` is
        ``[(index, expect_name, normalized), ...]``. As in :meth:`write_params`
        the read-back is recorded but **not compared** - establishing the effect
        is Core's job through the provider policy - and the setter's return value
        is recorded, never trusted.
        """
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)

        def _write() -> list[dict[str, Any]]:
            track = self._track(R, track_index)
            rows: list[dict[str, Any]] = []
            with self._hold():
                # Phase 1: EVERY index is name-checked before ANY setter runs. A moved
                # index therefore refuses with `writes_issued: 0` - a datum the caller
                # may rely on, because no setter call has been made at that point.
                checked: list[tuple[int, str, float]] = []
                for index, expect_name, value in pairs:
                    observed = self._param_name(R, track, fx_index, int(index))
                    if observed is None:
                        raise Unreadable(
                            "the host reports no parameter name at the addressed index",
                            detail={"index": int(index), "fx_index": fx_index, "writes_issued": 0},
                        )
                    if str(observed) != str(expect_name):
                        raise TargetChanged(
                            "the parameter at the addressed index is not the control this write was "
                            "planned against; the index moved",
                            detail={
                                "scene": "index_name_guard",
                                "index": int(index),
                                "expected_name": str(expect_name),
                                "observed_name": str(observed),
                                "writes_issued": 0,
                                "remedy": (
                                    "re-measure the instance (plugin_prepare) so the index/name "
                                    "binding is current, then retry"
                                ),
                            },
                        )
                    checked.append((int(index), str(expect_name), float(value)))
                # Phase 2: setters. The name is re-read immediately before EACH
                # setter, not only once for the batch: a plugin's parameter table
                # can move *while the batch runs* (a setter that switches a mode),
                # and a stale index would write the right number to the wrong
                # control. `writes_issued` reports how many setters had already
                # run, so a mid-batch refusal is accounted as a partly applied
                # batch rather than as "nothing happened".
                for index, expect_name, value in checked:
                    observed = self._param_name(R, track, fx_index, index)
                    if str(observed) != expect_name:
                        raise TargetChanged(
                            "the parameter table moved while the batch was running",
                            detail={
                                "scene": "index_name_guard_mid_batch",
                                "index": index,
                                "expected_name": expect_name,
                                "observed_name": observed,
                                "writes_issued": len(rows),
                            },
                        )
                    returned = R.TrackFX_SetParamNormalized(track, fx_index, index, value)
                    rows.append(
                        {
                            "index": index,
                            "expected_name": expect_name,
                            "observed_name": expect_name,
                            "sent": value,
                            "setter_returned": (
                                bool(returned[0])
                                if isinstance(returned, (list, tuple))
                                else returned
                            ),
                        }
                    )
                for row in rows:
                    row["read_back"] = float(
                        R.TrackFX_GetParamNormalized(track, fx_index, int(row["index"]))
                    )
                    # The formatter prints what the plugin *displays* for the value it
                    # now holds - one more call inside the same hop, and the only
                    # channel in which a caller can see whether a normalised write
                    # landed on the value they meant in the plugin's own terms.
                    formatted = R.TrackFX_FormatParamValueNormalized(
                        track, fx_index, int(row["index"]), float(row["read_back"]), "", 512
                    )
                    row["read_back_display"] = (
                        str(formatted[5]) if isinstance(formatted, (list, tuple)) and len(formatted) > 5 else None
                    )
            return rows

        rows = self.call(_write, timeout=self.timeout, label="write_params_by_index")
        return WriteAttempt(
            unit_index=-1,
            attempted_utc=utc_now_iso(),
            returned={"channel": "parameters", "address": "index", "rows": rows},
            effects_verified=None,  # established by Core's verify, never by the setter
        )

    def write_state_slice(
        self,
        ref: PluginInstanceRef,
        slice: TargetedStateSlice,
        new_block: TargetedStateSlice,
        *,
        timing: TimingProfile,
    ) -> WriteAttempt:
        """Splice ONLY the located span, write the whole chunk once, settle, re-read.

        No semantic callbacks: the setter's return value is recorded and never
        trusted, the settle/retry timing comes from the provider's declared
        profile, and the re-read is a transport-level read whose digest is
        recorded. Whether the write *landed* is decided by the codec from the
        slice's own bytes (10 §1.2).
        """
        self.ensure()
        R = self._api()
        track_index, _fx_index = _location(ref)

        def _write() -> dict[str, Any]:
            track = self._track(R, track_index)
            chunk = self._read_chunk(R, track)
            if not slice.is_valid_for(chunk):
                raise StateConflict(
                    "the state chunk changed since the slice was located; the slice is stale",
                    detail={
                        "slice_digest": slice.chunk_digest,
                        "chunk_digest": text_digest(chunk),
                    },
                )
            spliced = splice_block_text(chunk, slice, new_block.block_text)
            returned = R.SetTrackStateChunk(track, spliced, False)
            return {
                "setter_returned": returned,
                "pre_digest": text_digest(chunk),
                "sent_digest": text_digest(spliced),
                "sent_bytes": len(spliced),
            }

        data = self.call(_write, timeout=self.timeout, label="write_state_slice")

        self._settle(timing)
        retries = 0
        readback = self.read_state_chunk(ref)
        while (
            timing.retry_count > 0
            and retries < int(timing.retry_count)
            and text_digest(readback) == data["pre_digest"]
        ):
            self._sleeper(float(timing.retry_after_s))
            retries += 1
            readback = self.read_state_chunk(ref)
        located = self._locate_by_class(
            readback,
            class_hex=slice.class_hex,
            module=_header_module(slice.header_line),
            ordinal=slice.ordinal,
        )
        return WriteAttempt(
            unit_index=-1,
            attempted_utc=utc_now_iso(),
            returned={
                "channel": "state",
                "setter_returned": data["setter_returned"],
                "retries": retries,
                "sent_bytes": data["sent_bytes"],
                "sent_digest": data["sent_digest"],
            },
            slice_digest_sent=text_digest(new_block.block_text),
            readback_slice_digest=located.block_digest,
            effects_verified=None,  # the splice landing is Core's decode-and-compare
        )

    def re_read_slice(
        self, ref: PluginInstanceRef, facts: FxHostFacts, unit: StateWriteUnit
    ) -> TargetedStateSlice:
        """Read the chunk again and re-locate the slice a unit targets.

        Relocation is by identity facts, never by line numbers: a shifted chunk
        must produce a different, correctly located span (10 §1.2.6).
        """
        chunk = self.read_state_chunk(ref)
        return self.locate_state_slice(ref, facts, chunk)

    # -- write-path internals ----------------------------------------------

    def _read_chunk(self, R: Any, track: Any) -> str:
        raw = R.GetTrackStateChunk(track, "", _CHUNK_CAPACITY, False)
        return str(raw[2]) if isinstance(raw, (list, tuple)) and len(raw) > 2 else str(raw)

    def _settle(self, timing: TimingProfile) -> None:
        settle = float(getattr(timing, "settle_s", 0.0) or 0.0)
        if settle > 0:
            self._sleeper(settle)

    def _locate_by_class(
        self, chunk: str, *, class_hex: str | None, module: str | None, ordinal: int
    ) -> TargetedStateSlice:
        """Re-locate the just-written block by class **and module**, plus the ordinal.

        The module comes from the slice's own recorded header line. It is
        load-bearing: the locator computed ``ordinal`` among blocks matching
        class *and* module (the module string is what separates two builds that
        share a class id), so matching on the class alone could select a
        sibling block and report **its** digest as this write's read-back
        evidence - or refuse a write that landed.
        """
        if class_hex is None and module is None:
            raise Unreadable(
                "no identity evidence to re-locate the written block",
                detail={"class_hex": class_hex, "module": module},
            )
        blocks = parse_blocks(chunk)
        matching = [
            block
            for block in blocks
            if (class_hex is None or block["class_hex"] == class_hex.upper())
            and (module is None or block["module"] == module)
        ]
        if not matching:
            raise Unreadable(
                "the written block is not present in the read-back chunk",
                detail={"class_hex": class_hex, "module": module, "n_vst_blocks": len(blocks)},
            )
        if len(matching) == 1:
            if ordinal not in (0, None):
                raise TargetSliceAmbiguous(
                    "the read-back places the written block at a different position",
                    detail={"ordinal": ordinal, "matching_blocks": 1},
                )
            chosen = matching[0]
        elif ordinal is not None and 0 <= ordinal < len(matching):
            chosen = matching[ordinal]
        else:
            raise TargetSliceAmbiguous(
                "the read-back does not identify which matching block was written",
                detail={"ordinal": ordinal, "matching_blocks": len(matching)},
            )
        return TargetedStateSlice(
            chunk_digest=text_digest(chunk),
            line_start=chosen["line_start"],
            line_end=chosen["line_end"],
            header_line=chosen["header"],
            class_hex=chosen["class_hex"],
            ordinal=ordinal if len(matching) > 1 else 0,
            block_text=chosen["block_text"],
            framing=None,
        )

    def locate_state_slice(
        self,
        ref: PluginInstanceRef,
        facts: FxHostFacts,
        chunk: str,
        *,
        ordinal: int | None = None,
        matching_fx_count: int | None = None,
    ) -> TargetedStateSlice:
        """Unique-or-refuse location of the target's state block (AUD-001).

        When the caller has not supplied the correlation facts, the target's
        ordinal among the FX slots the host reports with the same name is read
        here - through the same timeout chokepoint as every other host call.
        """
        if ordinal is None and matching_fx_count is None and facts.fx_name:
            self.ensure()
            R = self._api()

            def _probe() -> tuple[int, int]:
                track = self._track(R, facts.track_index)
                return self._name_ordinal(R, track, facts.fx_index, facts.fx_name)

            ordinal, matching_fx_count = self.call_read(
                _probe, timeout=self.timeout, label="locate_state_slice"
            )
        return locate_block_span(
            chunk,
            class_hex=facts.class_id,
            module=facts.module,
            ordinal=ordinal,
            matching_fx_count=matching_fx_count,
        )

    # -- read-only metadata channels (P26) ---------------------------------

    def param_metadata(
        self,
        ref: PluginInstanceRef,
        indices: list[int],
        *,
        include: tuple[str, ...] = _METADATA_CHANNELS,
    ) -> dict[str, Any]:
        """Batched read-only metadata for a list of parameter indices.

        One host hop for the whole list. Every channel is optional and its
        *absence* is reported per parameter and per channel, because "this plugin
        has no default value" and "this host build has no such function" are
        different facts that must not collapse into an empty string.

        The read-only property of the formatter call is not assumed: it formats a
        value the caller passes and stores nothing (measured in P22 and re-proved
        in this round's live check).
        """
        self.ensure()
        R = self._api()
        wanted = tuple(dict.fromkeys(str(name) for name in include))
        track_index, fx_index = _location(ref)
        calls = 0

        def _read() -> dict[str, Any]:
            nonlocal calls
            track = self._track(R, track_index)
            rows: list[dict[str, Any]] = []
            channels: dict[str, str] = {}
            # The held context is what makes a batched read cheap: measured live
            # (P8) an unheld loop costs ~30 ms per call and the same loop held ~1 ms,
            # which is the difference between a 60 s timeout and a 3 s read for one
            # instance's whole parameter table (re-measured in P26: an unheld
            # 2858-parameter metadata read wedged at 60 s).
            with self._hold():
                for index in indices:
                    row: dict[str, Any] = {"index": int(index)}
                    if "name" in wanted:
                        row["name"] = self._param_name(R, track, fx_index, int(index))
                        calls += 1
                    if "ident" in wanted:
                        row["ident"] = self._ident(R, track, fx_index, int(index))
                        calls += 1
                    if "value" in wanted:
                        try:
                            row["value"] = float(R.TrackFX_GetParamNormalized(track, fx_index, int(index)))
                        except Exception:  # noqa: BLE001 - a missing value is a row, not a failure
                            row["value"] = None
                        calls += 1
                    if "steps" in wanted:
                        row["steps"] = self._step_sizes(R, track, fx_index, int(index))
                        calls += 1
                    if "formatted" in wanted:
                        row["formatted"] = self._formatted(R, track, fx_index, int(index))
                        calls += 1
                    if "named" in wanted or "named_full" in wanted:
                        named = self._named_config(
                            R, track, fx_index, int(index), full=("named_full" in wanted)
                        )
                        row["named_config"] = named
                        calls += 1 + len(named.get("probed") or [])
                    rows.append(row)
                if "sections" in wanted:
                    sections = self._param_sections(R, track, fx_index, indices)
                    channels["sections"] = sections["status"]
                    if sections["status"] == "ok":
                        by_index = {int(item["index"]): item.get("section") for item in sections["rows"]}
                        for row in rows:
                            row["section"] = by_index.get(int(row["index"]))
                    else:
                        channels["sections_reason"] = sections.get("reason") or ""
            return {"rows": rows, "channels": channels, "calls": calls, "include": list(wanted)}

        return self.call_read(_read, timeout=self.timeout, label="param_metadata")

    def param_sections(self, ref: PluginInstanceRef, indices: list[int]) -> dict[str, Any]:
        """The host's parameter-section (VST3 unit / CLAP module) names, if it has any.

        Section names are read from the *state block header* the host already
        reports for the instance rather than from a per-parameter call, so this
        verb answers even on a build without ``TrackFX_GetParamSectionName``. A
        build that has neither answers ``unsupported`` with the reason - never an
        empty map that would read as "no sections exist".
        """
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)
        function = getattr(R, "TrackFX_GetParamSectionName", None)

        def _read() -> dict[str, Any]:
            if callable(function):
                track = self._track(R, track_index)
                rows = []
                with self._hold():
                    for index in indices:
                        raw = list(function(track, fx_index, int(index), "", 512))
                        text = field_string(raw, 4) if len(raw) > 4 else None
                        rows.append({"index": int(index), "section": text or None})
                return {"status": "ok", "rows": rows, "source": "TrackFX_GetParamSectionName"}
            return {
                "status": "unsupported",
                "rows": [],
                "source": None,
                "reason": (
                    "this host build exposes no parameter-section read "
                    "(TrackFX_GetParamSectionName arrives in REAPER 7.82; the installed build is older)"
                ),
            }

        return self.call_read(_read, timeout=self.timeout, label="param_sections")

    def format_params_normalized(
        self, ref: PluginInstanceRef, pairs: list[tuple[int, float]]
    ) -> dict[str, Any]:
        """Format caller-supplied normalized values - one host hop, read-only.

        This is the display channel every discovery analysis reads. It never
        stores the value it formats, so a sweep cannot change the plugin state;
        the *engine* treats the returned text as the plugin's own statement about
        a value it would show.
        """
        self.ensure()
        R = self._api()
        track_index, fx_index = _location(ref)
        calls = 0

        def _read() -> dict[str, Any]:
            nonlocal calls
            track = self._track(R, track_index)
            texts: list[str | None] = []
            with self._hold():
                for index, value in pairs:
                    raw = list(
                        R.TrackFX_FormatParamValueNormalized(track, fx_index, int(index), float(value), "", 512)
                    )
                    calls += 1
                    texts.append(str(raw[5]) if len(raw) > 5 else None)
            return {"texts": texts, "calls": calls, "call": "TrackFX_FormatParamValueNormalized"}

        return self.call_read(_read, timeout=self.timeout, label="format_params_normalized")

    def _step_sizes(self, R: Any, track: Any, fx_index: int, index: int) -> dict[str, Any]:
        """``TrackFX_GetParameterStepSizes`` parsed into named fields."""
        function = getattr(R, "TrackFX_GetParameterStepSizes", None)
        if not callable(function):
            return {"status": "unsupported", "reported": False, "note": "the host has no step-size read"}
        try:
            raw = list(function(track, fx_index, index, 0.0, 0.0, 0.0, False))
        except Exception as exc:  # noqa: BLE001 - a refused probe is a row
            return {"status": "error", "reported": False, "note": f"{type(exc).__name__}: {exc}"}
        if len(raw) < 8:
            return {"status": "error", "reported": False, "note": "the host returned an unexpected tuple"}
        reported = bool(raw[0])
        return {
            "status": "ok" if (reported or bool(raw[7])) else "absent",
            "reported": reported,
            "step": float(raw[4]),
            "small": float(raw[5]),
            "large": float(raw[6]),
            "is_toggle": bool(raw[7]),
            "note": None if reported or bool(raw[7]) else "the host reports no step for this parameter",
        }

    def _formatted(self, R: Any, track: Any, fx_index: int, index: int) -> str | None:
        """``TrackFX_GetFormattedParamValue`` - the display text of the *current* value."""
        function = getattr(R, "TrackFX_GetFormattedParamValue", None)
        if not callable(function):
            return None
        try:
            raw = list(function(track, fx_index, index, "", _STRING_CAPACITY))
        except Exception:  # noqa: BLE001 - a missing display is a row
            return None
        return field_string(raw, 4) if len(raw) > 4 else None

    def _named_config(
        self, R: Any, track: Any, fx_index: int, index: int, *, full: bool = False
    ) -> dict[str, Any]:
        """Probe the documented per-parameter named-config keys.

        A key the plugin does not implement makes the host answer ``retval 0``; that
        is recorded as a *probed* key with no value, which is how "the plugin has
        no default" stays distinguishable from "nobody asked". Only the one key that
        can carry a value is probed by default: each probe is a host call, and the
        probe cost was measured (one unheld call ~30 ms) before this was narrowed.
        """
        function = getattr(R, "TrackFX_GetNamedConfigParm", None)
        result: dict[str, Any] = {"status": "unsupported", "entries": {}, "probed": []}
        if not callable(function):
            return result
        entries: dict[str, Any] = {}
        probed: list[str] = []
        keys = (
            ("default_value", "default_value"),
            ("min", "min"),
            ("max", "max"),
            ("units", "units"),
        ) if full else (("default_value", "default_value"),)
        for suffix, field_name in keys:
            key = f"param.{index}.{suffix}"
            probed.append(key)
            try:
                raw = list(function(track, fx_index, key, "", 256))
            except Exception:  # noqa: BLE001 - a refused key is a row
                continue
            if raw and bool(raw[0]):
                text = field_string(raw, 4) if len(raw) > 4 else None
                if text not in (None, ""):
                    entries[field_name] = text
        result.update(
            status="ok" if entries else "absent",
            entries=entries,
            probed=probed,
            note=None if entries else "the plugin answered none of the probed named-config keys",
        )
        return result

    def _param_sections(self, R: Any, track: Any, fx_index: int, indices: list[int]) -> dict[str, Any]:
        function = getattr(R, "TrackFX_GetParamSectionName", None)
        if not callable(function):
            return {
                "status": "unsupported",
                "rows": [],
                "reason": "this host build exposes no parameter-section read",
            }
        rows = []
        for index in indices:
            try:
                raw = list(function(track, fx_index, int(index), "", 512))
            except Exception:  # noqa: BLE001
                continue
            text = field_string(raw, 4) if len(raw) > 4 else None
            rows.append({"index": int(index), "section": text or None})
        return {"status": "ok", "rows": rows}


def _location(ref: PluginInstanceRef) -> tuple[int, int]:
    location = ref.location if isinstance(ref.location, dict) else {}
    return int(location.get("track_index", 0)), int(location.get("fx_index", 0))


def _header_module(header_line: str) -> str | None:
    """The module string of a block header (same rule the locator uses)."""
    match = _MODULE_RE.search(header_line or "")
    return match.group(1) if match else None


def _ident_index(R: Any, track: Any, fx_index: int, ident: str) -> int | None:
    """Resolve a parameter ident to its current index (inside the caller's hop).

    The host returns a plain index (``< 0`` when the ident is unknown); the
    tolerant unpacking mirrors the read helpers, which receive tuples on some
    API bindings.
    """
    raw = R.TrackFX_GetParamFromIdent(track, fx_index, str(ident))
    value = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None
