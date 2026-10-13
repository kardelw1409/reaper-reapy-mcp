"""HostBridge contract (contracts 05 §3 and 08 §4).

The bridge owns host-level transport and NOTHING semantic: it locates, splices,
writes, settles and re-reads. It contains no provider predicate and no
verification callback (AUD-004), and it never imports a provider.

P3 implements the read half of this contract in ``reaper.py``; the write verbs
are implemented in P5. The full protocol is declared here once so the engine,
the mocks and the conformance suites all bind to the same shape.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from ..model import (
    FxHostFacts,
    PluginInstanceRef,
    StateWriteUnit,
    TargetedStateSlice,
    TimingProfile,
    TrackView,
    WriteAttempt,
)

__all__ = ["HostBridge"]


@runtime_checkable
class HostBridge(Protocol):
    """Host-level transport contract (no plugin semantics)."""

    # -- reads -------------------------------------------------------------

    def list_tracks(self) -> list[TrackView]:
        """Enumerate tracks (host-level; no plugin knowledge)."""
        ...

    def fx_facts(self, track_index: int, fx_index: int) -> FxHostFacts:
        """Everything identity needs, read fresh (name, class, module, format,
        version, param_count, slot count, session handle evidence)."""
        ...

    def param_idents(self, ref: PluginInstanceRef, indices: list[int]) -> list[str | None]:
        """Durable parameter idents for indices; ``None`` where unavailable."""
        ...

    def param_index_from_idents(self, ref: PluginInstanceRef, idents: list[str]) -> list[int | None]:
        """The current index of each ident; ``None`` where the ident is unknown.

        Additive read verb (P36): resolution **at use time**, the same rule the
        index-addressed write hop applies inside its own held hop, instead of a
        whole-table ident sweep per lookup.
        """
        ...

    def param_names(self, ref: PluginInstanceRef, indices: list[int]) -> list[str | None]:
        """Host-reported parameter names for indices; ``None`` where unavailable.

        Additive read verb (P8, 07 §3.6): the live **binding check** compares
        this name against the declared control's names, so an index that no
        longer addresses the control it was measured on refuses the write
        instead of writing a different parameter and verifying it.
        """
        ...

    def read_params(self, ref: PluginInstanceRef, indices: list[int]) -> list[float]:
        """Normalized parameter values; ONE batched host hop per call."""
        ...

    # -- read-only metadata channels (P26; additive) ------------------------

    def param_metadata(
        self, ref: PluginInstanceRef, indices: list[int], *, include: tuple[str, ...] = ...
    ) -> dict[str, Any]:
        """Batched read-only metadata: name, ident, current value, step sizes,
        current formatted text, named-config entries.

        One host hop for the whole list. Each channel is optional and its
        absence is reported **per channel and per parameter** (``status``
        ``ok``/``absent``/``unsupported``/``error``): a plugin with no default
        value and a host with no such function are different facts.
        """
        ...

    def param_sections(self, ref: PluginInstanceRef, indices: list[int]) -> dict[str, Any]:
        """Parameter section (VST3 unit / CLAP module) names when the host build
        exposes a channel for them; otherwise ``unsupported`` with the reason."""
        ...

    def format_params_normalized(
        self, ref: PluginInstanceRef, pairs: list[tuple[int, float]]
    ) -> dict[str, Any]:
        """The host's display text for caller-supplied normalized values.

        Read-only by construction: the host formats a value the caller passes and
        stores nothing (measured in P22/P23 and re-proved by this round's live
        check). This is the channel every display-space analysis reads.
        """
        ...

    def read_state_chunk(self, ref: PluginInstanceRef) -> str:
        """The host's state transport payload for the addressed track."""
        ...

    def locate_state_slice(
        self,
        ref: PluginInstanceRef,
        facts: FxHostFacts,
        chunk: str,
        *,
        ordinal: int | None = None,
        matching_fx_count: int | None = None,
    ) -> TargetedStateSlice:
        """Locate the exact block span uniquely correlated to the target.

        The correlation is decided from host facts (class id, module, ordinal
        among matching FX/blocks) and must be unique: an ambiguous or
        inconsistent correlation raises ``TargetSliceAmbiguous`` and writes
        nothing (AUD-001). ``ordinal``/``matching_fx_count`` are supplied by
        the caller when the host enumerates the track's FX slots (needed to
        address one of several identical instances on one track).
        """
        ...

    # -- writes (P5; not implemented by the P3 read bridge) ----------------

    def write_params(
        self, ref: PluginInstanceRef, pairs: list[tuple[str, float]]
    ) -> WriteAttempt:
        """One batched ident-addressed parameter write with read-back."""
        ...

    def write_state_slice(
        self,
        ref: PluginInstanceRef,
        slice: TargetedStateSlice,
        new_block: TargetedStateSlice,
        *,
        timing: TimingProfile,
    ) -> WriteAttempt:
        """Splice ONLY the selected span into the freshly read chunk, write the
        whole chunk once, settle, re-read. No semantic callbacks (AUD-004)."""
        ...

    def re_read_slice(
        self, ref: PluginInstanceRef, facts: FxHostFacts, unit: StateWriteUnit
    ) -> TargetedStateSlice:
        """Read the chunk again and re-locate the slice that ``unit`` targets."""
        ...

    # -- transport discipline ---------------------------------------------

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float, label: str) -> Any:
        """The single timeout/wedge chokepoint for every host call.

        A hung host must never hang the server: the call is bounded by
        ``timeout`` and classified as ``Wedged``; transport death is
        ``NotConnected`` (05 §7).
        """
        ...

    def reconnect(self) -> None:
        """Explicit reconnect after a classified transport failure."""
        ...
