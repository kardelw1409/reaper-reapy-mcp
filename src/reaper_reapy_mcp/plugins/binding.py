"""Live binding of parameter idents: how a declared control becomes a write address (P8).

Three different facts have to line up before a parameter write may be issued,
and none of them is sufficient alone (07 §3.6, 08 §4.3):

1. the provider declares, per control, the **index** its registry measured
   (`binding.index`) and the **names** the host reports for that parameter
   (`label`, `id` and `aliases`);
2. the host reports, at that index, a parameter **name** - the invariant that
   makes the index trustworthy: an index is only valid for the build it was
   measured on, so in a reordered or different build it addresses a *different*
   control, and a verified read-back of the wrong parameter would look exactly
   like success;
3. the host reports, at that index, a durable **ident** - the only thing the
   write hop resolves back to an index (`TrackFX_GetParamFromIdent`), because
   indices go stale between hops.

Core performs this check (the provider never reads the host, the bridge never
sees provider vocabulary). A control whose live name does not match, whose index
is absent, or whose ident is unreadable stays **unbound**: the request is
refused (`UnboundControl`) instead of silently writing nothing or writing a
different parameter.

Measured live shape (P8, 2026-10-10), recorded so the rule is not a guess:

- index 0 -> name ``"Beats Per Minute"``, ident ``"0:48"``;
- the registry row for that index carries the same name and the state key
  ``beats_per_minute``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "normalise_name",
    "declared_names",
    "indexed_controls",
    "unindexed_controls",
    "BindingReport",
    "bind_profile",
]


def normalise_name(value: Any) -> str:
    """Case- and whitespace-insensitive form used for **every** name comparison.

    Deliberately conservative: no stemming, no fuzzy matching, no substring
    rule. A near-miss must stay a refused write, not become a guessed binding.
    """
    return " ".join(str(value or "").split()).casefold()


def declared_names(control: Mapping[str, Any]) -> list[str]:
    """Every name the profile declares for one control, normalised."""
    raw: list[Any] = [control.get("label"), control.get("id")]
    raw.extend(control.get("aliases") or [])
    binding = control.get("binding") or {}
    raw.append(binding.get("state_key"))
    names: list[str] = []
    for item in raw:
        key = normalise_name(item)
        if key and key not in names:
            names.append(key)
    return names


def _int_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def indexed_controls(profile: Mapping[str, Any]) -> list[tuple[str, int]]:
    """``(control_id, declared index)`` for every control declaring a numeric index."""
    rows: list[tuple[str, int]] = []
    for control in profile.get("controls") or []:
        index = _int_index((control.get("binding") or {}).get("index"))
        if index is None:
            continue
        rows.append((str(control.get("id")), index))
    return rows


def unindexed_controls(profile: Mapping[str, Any]) -> list[str]:
    """Controls with no declared index: they can never be host-addressed."""
    return [
        str(control.get("id"))
        for control in profile.get("controls") or []
        if _int_index((control.get("binding") or {}).get("index")) is None
    ]


@dataclass
class BindingReport:
    """What the live binding check measured, for ``meta`` and for evidence."""

    controls_indexed: int = 0
    bound: int = 0
    declared_ident_kept: int = 0
    ident_refreshed: int = 0
    unverified: list[dict[str, Any]] = field(default_factory=list)
    refused_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "controls_indexed": self.controls_indexed,
            "bound": self.bound,
            "declared_ident_kept": self.declared_ident_kept,
            "ident_refreshed": self.ident_refreshed,
            "unverified": list(self.unverified),
            "refused_reasons": dict(self.refused_reasons),
        }

    def _refuse(self, control_id: str, index: int, reason: str, **detail: Any) -> None:
        self.unverified.append({"control": control_id, "index": index, "reason": reason, **detail})
        self.refused_reasons[reason] = self.refused_reasons.get(reason, 0) + 1


def bind_profile(
    profile: Mapping[str, Any],
    *,
    indexed: Sequence[tuple[str, int]],
    names: Sequence[str | None],
    idents: Sequence[str | None],
) -> tuple[dict[str, Any], BindingReport]:
    """Return a profile copy with **verified** live ident bindings, plus a report.

    ``names``/``idents`` are the host's answers for ``indexed``, positionally
    aligned; the caller (the engine) guarantees their length. The input profile
    is never mutated - a binding is live runtime state, and a cached profile must
    not start carrying one.

    A control is bound only when the host's own name at the declared index equals
    one of the names the profile declares for it. Everything else is recorded
    with the concrete reason, and the control is left **unbound** so that the
    request is refused rather than mis-addressed.
    """
    bound_profile = copy.deepcopy(dict(profile))
    by_id = {str(control.get("id")): control for control in bound_profile.get("controls") or []}
    report = BindingReport(controls_indexed=len(indexed))
    for position, (control_id, index) in enumerate(indexed):
        control = by_id.get(control_id)
        if control is None:  # the caller's list came from this profile; defensive only
            report._refuse(control_id, index, "control_absent")
            continue
        live_name = names[position] if position < len(names) else None
        live_ident = idents[position] if position < len(idents) else None
        if live_name is None:
            report._refuse(control_id, index, "name_unreadable")
        elif normalise_name(live_name) not in declared_names(control):
            report._refuse(
                control_id,
                index,
                "name_mismatch",
                observed_name=str(live_name),
                declared_names=sorted(declared_names(control)),
            )
        elif live_ident is None:
            report._refuse(control_id, index, "ident_unreadable", observed_name=str(live_name))
        else:
            binding = control.setdefault("binding", {})
            declared_ident = binding.get("ident")
            if declared_ident == live_ident:
                report.declared_ident_kept += 1
            elif declared_ident:
                report.ident_refreshed += 1
            binding["ident"] = str(live_ident)
            binding["ident_source"] = "live_verified"
            binding["measured_name"] = str(live_name)
            report.bound += 1
            continue
        # unbound: the reason travels with the control so the refusal can name it
        binding = control.setdefault("binding", {})
        binding["ident"] = None
        binding["unverified"] = {
            "reason": report.unverified[-1]["reason"],
            "index": index,
            "observed_name": live_name,
            "declared_names": sorted(declared_names(control)),
        }
    return bound_profile, report
