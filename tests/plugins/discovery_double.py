"""Offline double for the P26 discovery channels (test support).

The discovery engine's only host-facing seam is the bridge, so a test can drive
the *whole* pipeline - inventory, sweep, analyzers, classification, module
inference, artifact write - without REAPER by implementing three verbs. This
double implements exactly those three and counts every call, which is what makes
"the second call measured nothing" a measurable property instead of a promise.

It is deliberately *not* a mock of `MockHostBridge`: the real bridge is covered by
its own conformance suite, and a double that reimplemented it would hide a change
in the channel shapes. This one speaks only the P26 metadata shapes.

P28 added the four verbs the providerless *tool* path uses (`param_names`,
`read_params`, `param_idents`, `write_params_by_index`): describing, searching,
reading and writing an instance whose prepared artifact exists. They are
implemented here - not in `MockHostBridge` - because the discovery path's
address space is the artifact's index/name pair, and the double has to be able
to make that pair *move* (``name_shift``) and to make a write *not land*
(``refuse_writes``) for the two failure tests.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from reaper_reapy_mcp.plugins.model import FxHostFacts

#: A display function: ``(index, normalized) -> text``.
DisplayFn = Callable[[int, float], str]


def _numeric_display(scale: float, offset: float, unit: str = "", decimals: int = 2) -> DisplayFn:
    def display(index: int, normalized: float) -> str:
        value = offset + scale * float(normalized)
        return f"{value:.{decimals}f}{unit}"

    return display


def linear_display(low: float, high: float, unit: str = "", decimals: int = 2) -> DisplayFn:
    return _numeric_display(high - low, low, unit, decimals)


def power_display(scale: float, exponent: float, unit: str = "", decimals: int = 4) -> DisplayFn:
    def display(index: int, normalized: float) -> str:
        return f"{scale * float(normalized) ** exponent:.{decimals}f}{unit}"

    return display


def labels_display(labels: Sequence[str]) -> DisplayFn:
    """A banded display: member ``k`` occupies ``[k/n, (k+1)/n)``."""

    def display(index: int, normalized: float) -> str:
        count = max(1, len(labels))
        ordinal = min(count - 1, int(float(normalized) * count))
        return labels[ordinal]

    return display


def stepper_display(step: float, low: float = 0.0, unit: str = "") -> DisplayFn:
    """An integer-stepping display (the ladder / index pattern)."""

    def display(index: int, normalized: float) -> str:
        value = low + step * round(float(normalized) * 20.0)
        return f"{value:g}{unit}"

    return display


class FakeControl:
    """One fake parameter: identity, display and the host's declared metadata."""

    def __init__(
        self,
        index: int,
        name: str,
        *,
        display: DisplayFn | None = None,
        ident: str | None = None,
        step: float = 0.0,
        is_toggle: bool = False,
        named: Mapping[str, Any] | None = None,
        section: str | None = None,
    ) -> None:
        self.index = int(index)
        self.name = name
        self.ident = ident if ident is not None else f"0:{index}"
        self.display = display or linear_display(0.0, 1.0, decimals=3)
        self.step = float(step)
        self.is_toggle = bool(is_toggle)
        self.named = dict(named or {})
        self.section = section


class FakeDiscoveryBridge:
    """Three P26 verbs over a list of fake controls, with a call census."""

    def __init__(self, controls: Iterable[FakeControl], *, fail_format: bool = False) -> None:
        self.controls = {control.index: control for control in controls}
        self.calls: dict[str, int] = {
            "param_metadata": 0,
            "format_params_normalized": 0,
            "param_sections": 0,
            "param_names": 0,
            "param_idents": 0,
            "read_params": 0,
            "write_params_by_index": 0,
        }
        self.values: dict[int, float] = {index: 0.0 for index in self.controls}
        self.fail_format = bool(fail_format)
        self.metadata_calls: list[int] = []
        self.format_batches: list[int] = []
        self.track_name = "Track 1"
        #: Simulate a write the plugin ignores (the read-back cannot agree).
        self.refuse_writes = False
        #: Simulate a parameter table that moved: ``{index: new_name}``.
        self.name_shift: dict[int, str] = {}
        #: Every index-addressed batch, as ``(index, expected_name, value)`` rows.
        self.index_write_rows: list[tuple[int, str, float]] = []

    # -- the P28 tool path's address space -----------------------------

    def param_names(self, ref: Any, indices: Sequence[int]) -> list[str | None]:
        self.calls["param_names"] += 1
        return [self._name(int(index)) for index in indices]

    def param_idents(self, ref: Any, indices: Sequence[int]) -> list[str | None]:
        self.calls["param_idents"] += 1
        return [
            (self.controls[int(index)].ident if int(index) in self.controls else None)
            for index in indices
        ]

    def param_index_from_idents(self, ref: Any, idents: Sequence[str]) -> list[int | None]:
        """P36: resolution at use time (the real verb's direct rule)."""
        self.calls["param_index_from_idents"] = self.calls.get("param_index_from_idents", 0) + 1
        by_ident = {control.ident: index for index, control in self.controls.items()}
        return [by_ident.get(str(ident)) for ident in idents]

    def read_params(self, ref: Any, indices: Sequence[int]) -> list[float]:
        self.calls["read_params"] += 1
        return [float(self.values.get(int(index), 0.0)) for index in indices]

    def write_params_by_index(
        self, ref: Any, pairs: Sequence[tuple[int, str, float]]
    ) -> Any:
        """The index-addressed, live-name-guarded write (P28 host verb).

        Mirrors the real bridge's contract: the name at the index is compared to
        the expected one **before** that parameter's setter runs, the setter's
        return value is recorded and never trusted, and the read-back (plus the
        formatter's text) is recorded for the caller's verification.
        """
        from reaper_reapy_mcp.plugins.errors import TargetChanged
        from reaper_reapy_mcp.plugins.model import WriteAttempt, utc_now_iso

        self.calls["write_params_by_index"] += 1
        rows: list[dict[str, Any]] = []
        # Phase 1: every index is name-checked before any setter runs, exactly as
        # the real verb does, so a moved index refuses with `writes_issued: 0`.
        checked: list[tuple[int, str, float]] = []
        for index, expect_name, value in pairs:
            index = int(index)
            observed = self._name(index)
            if observed != str(expect_name):
                raise TargetChanged(
                    "the parameter at the addressed index is not the control this write was "
                    "planned against; the index moved",
                    detail={
                        "scene": "index_name_guard",
                        "index": index,
                        "expected_name": str(expect_name),
                        "observed_name": observed,
                        "writes_issued": 0,
                    },
                )
            checked.append((index, str(expect_name), float(value)))
        for index, expect_name, value in checked:
            # The real verb re-reads the name immediately before each setter (a
            # table can move mid-batch); the double does the same.
            observed_now = self._name(index)
            if observed_now != expect_name:
                raise TargetChanged(
                    "the parameter table moved while the batch was running",
                    detail={
                        "scene": "index_name_guard_mid_batch",
                        "index": index,
                        "expected_name": expect_name,
                        "observed_name": observed_now,
                        "writes_issued": len(self.index_write_rows),
                    },
                )
            if not self.refuse_writes:
                self.values[index] = float(value)
            rows.append(
                {
                    "index": index,
                    "expected_name": expect_name,
                    "observed_name": expect_name,
                    "sent": value,
                    "setter_returned": not self.refuse_writes,
                    "read_back": float(self.values[index]),
                    "read_back_display": self._display(index, float(self.values[index])),
                }
            )
            self.index_write_rows.append((index, expect_name, value))
        return WriteAttempt(
            unit_index=-1,
            attempted_utc=utc_now_iso(),
            returned={"channel": "parameters", "address": "index", "rows": rows},
            effects_verified=None,
        )

    def _name(self, index: int) -> str | None:
        if index is None or int(index) not in self.controls:
            return None
        return self.name_shift.get(int(index), self.controls[int(index)].name)

    def _display(self, index: int, value: float) -> str | None:
        control = self.controls.get(int(index))
        return None if control is None else control.display(int(index), float(value))

    # -- host-level reads the tool layer needs to address a slot -----------

    def list_tracks(self) -> list[Any]:
        from reaper_reapy_mcp.plugins.model import TrackView

        return [TrackView(track_index=0, name=self.track_name, n_fx=1)]

    def fx_facts(self, track_index: int, fx_index: int) -> Any:
        return facts_for(param_count=len(self.controls))

    # -- the P26 contract --------------------------------------------------

    def param_metadata(self, ref: Any, indices: Sequence[int], *, include: tuple[str, ...] = ()) -> dict[str, Any]:
        wanted = list(indices)
        self.calls["param_metadata"] += 1
        self.metadata_calls.append(len(wanted))
        rows = []
        for index in wanted:
            control = self.controls.get(int(index))
            if control is None:
                continue
            rows.append(
                {
                    "index": int(index),
                    "name": control.name,
                    "ident": control.ident,
                    "value": self.values.get(int(index)),
                    "steps": {
                        "status": "ok" if control.step or control.is_toggle else "absent",
                        "reported": bool(control.step),
                        "step": control.step,
                        "small": control.step,
                        "large": control.step,
                        "is_toggle": control.is_toggle,
                    },
                    "formatted": control.display(int(index), float(self.values.get(int(index), 0.0))),
                    "named_config": {
                        "status": "ok" if control.named else "absent",
                        "entries": dict(control.named),
                        "probed": sorted(control.named),
                    },
                    "section": control.section,
                }
            )
        return {"rows": rows, "channels": {}, "calls": len(rows)}

    def param_sections(self, ref: Any, indices: Sequence[int]) -> dict[str, Any]:
        self.calls["param_sections"] += 1
        if not any(control.section for control in self.controls.values()):
            return {"status": "unsupported", "rows": [], "reason": "the double declares no parameter sections"}
        rows = [
            {"index": int(index), "section": self.controls[int(index)].section}
            for index in indices
            if int(index) in self.controls
        ]
        return {"status": "ok", "rows": rows, "source": "TrackFX_GetParamSectionName"}

    def format_params_normalized(self, ref: Any, pairs: Sequence[tuple[int, float]]) -> dict[str, Any]:
        self.calls["format_params_normalized"] += 1
        self.format_batches.append(len(pairs))
        if self.fail_format:
            return {"texts": [], "calls": 0, "note": "the double was told to fail this channel"}
        texts: list[str | None] = []
        for index, value in pairs:
            control = self.controls.get(int(index))
            texts.append(None if control is None else control.display(int(index), float(value)))
        return {"texts": texts, "calls": len(pairs), "call": "TrackFX_FormatParamValueNormalized"}


def facts_for(
    *,
    fx_name: str = "Fake Synth",
    class_id: str = "0" * 31 + "1",
    module: str = "fake.vst3",
    param_count: int | None = None,
    track_index: int = 0,
    fx_index: int = 0,
) -> FxHostFacts:
    return FxHostFacts(
        track_index=track_index,
        fx_index=fx_index,
        fx_name=fx_name,
        class_id=class_id,
        module=module,
        param_count=param_count,
        version="1.0.0",
    )


_BANNED = ("vital", "osc_", "filter_1")


def assert_no_provider_tokens(text: str, *, source: str = "") -> None:
    """The L0 boundary rule, reusable by the P26 independence tests."""
    lowered = text.lower()
    for token in _BANNED:
        assert token not in lowered, f"{source}: contains provider identifier {token!r}"


def tokenize_free_names(prefix: str, count: int, *, suffix: str = "control") -> list[str]:
    """Neutral parameter names for grouping tests (no plugin vocabulary)."""
    return [f"{prefix}{index}_{suffix}" for index in range(1, count + 1)]


_NUMBERISH = re.compile(r"^\d+(?:\.\d+)?$")


def is_numberish(text: str) -> bool:
    return bool(_NUMBERISH.match(text.strip()))
