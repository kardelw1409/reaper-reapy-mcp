"""Format adapters: native metadata when a channel is actually reachable (P26 §7).

The honest answer for ReaScript is: **it depends on the host version and the
plugin's format**, and the only way to know is to ask the running API surface.
So an adapter here never advertises a capability from documentation; it declares
the host functions it would use and reports, per instance, whether they exist and
what they answered.

Two facts this module is built around (both measured on this machine, P21/P24):

- ``TrackFX_GetParamSectionName`` / ``TakeFX_GetParamSectionName`` exist in
  REAPER 7.82 and are **absent in 7.53**, so the section channel is probed, not
  assumed;
- ``TrackFX_GetNamedConfigParm "param.<i>.default_value"`` is documented as
  per-plugin; a plugin without defaults answers ``retval 0`` for every parameter,
  which is a fact about the *plugin*, and is recorded as such.

No adapter blocks the generic pipeline: without one, the inventory still runs and
every property that needed native metadata is reported as unknown with the
reason ``format_adapter_unavailable``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

__all__ = [
    "ADAPTER_CAPABILITIES",
    "AdapterProbe",
    "FormatAdapter",
    "ReaScriptAdapter",
    "available_adapters",
    "probe_adapters",
]

#: What a format adapter can contribute to discovery.
ADAPTER_CAPABILITIES: tuple[str, ...] = (
    "range",  # numeric bounds in the plugin's own space
    "step",  # the plugin's own step/quantisation
    "default",  # the plugin's own default value
    "units",  # a unit string the plugin declares
    "enum",  # member labels/values
    "stable_id",  # a durable identifier
    "group",  # units/sections the plugin declares
    "display",  # a native value -> text formatter
)


@dataclass
class AdapterProbe:
    """One adapter's answer for one instance."""

    adapter: str
    format: str
    status: str  # available | unavailable | partial | error | not_attempted
    functions: dict[str, bool] = field(default_factory=dict)
    provides: list[str] = field(default_factory=list)
    rows: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "format": self.format,
            "status": self.status,
            "functions": dict(self.functions),
            "provides": list(self.provides),
            "rows": dict(self.rows),
            "reason": self.reason,
        }


@runtime_checkable
class FormatAdapter(Protocol):
    """A native-metadata channel for one or more plugin formats."""

    id: str
    formats: tuple[str, ...]
    capabilities: tuple[str, ...]

    def probe(self, api: Any, ref: Any, facts: Any) -> AdapterProbe:
        """Whether this channel exists for this instance, and what it answered."""
        ...


def _function_names(api: Any, prefixes: Sequence[str], suffixes: Sequence[str]) -> dict[str, bool]:
    """Which of the requested host functions the live API surface exposes."""
    surface: dict[str, bool] = {}
    for prefix in prefixes:
        for suffix in suffixes:
            surface[f"{prefix}{suffix}"] = callable(getattr(api, prefix + suffix, None))
    return surface


class ReaScriptAdapter:
    """The channel set available to ReaScript for any plugin format.

    One adapter for VST3/VST2/CLAP/AU on purpose: from ReaScript the *format* is
    not what decides which metadata is reachable - the host's generic parameter
    API is. Splitting it into four classes that share one implementation would
    suggest a distinction the engine cannot observe.
    """

    id = "reascript-generic"
    formats = ("vst3", "vst2", "clap", "au", "unknown")
    capabilities = ("step", "default", "group", "stable_id")

    def probe(self, api: Any, ref: Any, facts: Any) -> AdapterProbe:
        functions = _function_names(
            api,
            ("TrackFX_", "TakeFX_"),
            (
                "GetParamSectionName",
                "GetNamedConfigParm",
                "GetParameterStepSizes",
                "GetFormattedParamValue",
                "FormatParamValueNormalized",
                "GetParamFromIdent",
            ),
        )
        section = functions.get("TrackFX_GetParamSectionName", False)
        named = functions.get("TrackFX_GetNamedConfigParm", False)
        provides = [name for name in self.capabilities if name in ("step", "stable_id")]
        if section:
            provides.append("group")
        if named:
            provides.append("default")
        probe = AdapterProbe(
            adapter=self.id,
            format=str(getattr(facts, "format", "") or "unknown").lower() or "unknown",
            status="partial" if provides else "unavailable",
            functions=functions,
            provides=sorted(set(provides)),
        )
        if not section and not named:
            probe.status = "unavailable"
            probe.reason = (
                "this host build exposes neither a parameter-section read nor a named-config read, "
                "so no native group/default metadata is reachable"
            )
        elif not section:
            probe.reason = "the host build has no parameter-section read (available only from REAPER 7.82)"
        return probe


class NoNativeAdapter:
    """Declared absence: the adapter a caller installs to state that a format has
    no reachable native channel at all. Always reports unavailable, with the
    reason, so a caller never mistakes silence for an unmeasured capability."""

    id = "none"
    formats = ("vst3", "vst2", "clap", "au", "unknown")
    capabilities = ()

    def probe(self, api: Any, ref: Any, facts: Any) -> AdapterProbe:
        return AdapterProbe(
            adapter=self.id,
            format="unknown",
            status="unavailable",
            provides=[],
            reason="no native-metadata adapter is installed for this format",
        )


def available_adapters(ids: Sequence[str] | None = None) -> list[FormatAdapter]:
    """The adapters the engine will try, in order.

    ``ids`` selects a subset; an unknown id raises so a configuration typo cannot
    silently reduce coverage.
    """
    registry: dict[str, FormatAdapter] = {
        "reascript-generic": ReaScriptAdapter(),
        "none": NoNativeAdapter(),
    }
    if ids is None:
        return [registry["reascript-generic"]]
    selected: list[FormatAdapter] = []
    for adapter_id in ids:
        key = str(adapter_id).strip().lower()
        if key not in registry:
            raise ValueError(f"no format adapter is known by the id {adapter_id!r}")
        selected.append(registry[key])
    return selected


def probe_adapters(
    api: Any,
    ref: Any,
    facts: Any,
    *,
    adapters: Sequence[FormatAdapter] | None = None,
) -> list[dict[str, Any]]:
    """Probe every adapter and return rows for the artifact."""
    rows: list[dict[str, Any]] = []
    for adapter in adapters or available_adapters():
        try:
            probe = adapter.probe(api, ref, facts)
        except Exception as exc:  # noqa: BLE001 - a probe failure is a row, not a crash
            probe = AdapterProbe(
                adapter=getattr(adapter, "id", "unknown"),
                format="unknown",
                status="error",
                reason=f"{type(exc).__name__}: {exc}",
            )
        rows.append(probe.to_dict())
    return rows
