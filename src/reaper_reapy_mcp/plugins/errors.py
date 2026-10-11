"""Typed plugin-layer errors (Universal Plugin MCP V3).

Normative source: docs/universal-plugin-mcp-v3/05-universal-core-contract.md §7.
The list is CLOSED for V1: every error the plugin layer raises is one of the
classes declared below, every class carries a stable ``code`` string, and every
class serializes as ``{code, message, detail}`` for the tool envelope's
``error`` object (06 §2, schema 2).

Nothing in this module may import a host module and nothing may contain a
provider identifier (08 §8 boundary rules).
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "PluginError",
    # transport / host
    "NotConnected",
    "Wedged",
    "Unreadable",
    "HostContractUnavailable",
    # domain / resolution
    "InstanceNotFound",
    "AmbiguousInstance",
    "StaleInstance",
    "WrongPlugin",
    "VersionMismatch",
    "UnsupportedControl",
    "InvalidValue",
    "NoConversion",
    "InvalidRoute",
    "InvalidSourceFamily",
    "DependencyViolation",
    "IncompatibleSettings",
    "PlanError",
    "UnsupportedProvider",
    "TargetSliceAmbiguous",
    # conflict / identity
    "TargetChanged",
    "StateConflict",
    "SnapshotUnavailable",
    # write outcomes
    "WriteNotApplied",
    "StateMismatch",
    "ReadbackMismatch",
    "WriteOrderViolation",
    "RollbackFailed",
    "RestoreIncomplete",
    # helpers
    "ERROR_CODES",
    "error_code",
]


class PluginError(Exception):
    """Base class for every typed plugin-layer error.

    ``code`` is the stable public identifier used in the envelope; by
    convention it equals the class name. ``detail`` carries the structured
    evidence the caller needs (never prose-only), and is JSON-serializable.
    """

    code: ClassVar[str] = "PluginError"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = dict(detail) if detail else {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        return f"{type(self).__name__}({self.message!r}, detail={self.detail!r})"


# ---------------------------------------------------------------------------
# Transport / host (raised by HostBridge)
# ---------------------------------------------------------------------------


class NotConnected(PluginError):
    """The host connection is not available (or was lost after startup)."""

    code: ClassVar[str] = "NotConnected"


class Wedged(PluginError):
    """A host call did not return within its hard timeout."""

    code: ClassVar[str] = "Wedged"


class Unreadable(PluginError):
    """A host value or state payload could not be read or decoded."""

    code: ClassVar[str] = "Unreadable"


class HostContractUnavailable(PluginError):
    """The host API surface the bridge needs is not available."""

    code: ClassVar[str] = "HostContractUnavailable"


# ---------------------------------------------------------------------------
# Domain / resolution (raised by Core / Provider)
# ---------------------------------------------------------------------------


class InstanceNotFound(PluginError):
    """No plugin instance matches the selector."""

    code: ClassVar[str] = "InstanceNotFound"


class AmbiguousInstance(PluginError):
    """More than one instance matches and no selector disambiguates."""

    code: ClassVar[str] = "AmbiguousInstance"


class StaleInstance(PluginError):
    """The resolved fingerprint no longer matches the live instance."""

    code: ClassVar[str] = "StaleInstance"


class WrongPlugin(PluginError):
    """The target slot holds a plugin the provider does not serve."""

    code: ClassVar[str] = "WrongPlugin"


class VersionMismatch(PluginError):
    """The live plugin version is not in the provider's measured set."""

    code: ClassVar[str] = "VersionMismatch"


class UnsupportedControl(PluginError):
    """A requested control/module/route name is unknown or unsupported."""

    code: ClassVar[str] = "UnsupportedControl"


class InvalidValue(PluginError):
    """A requested value is out of range, of the wrong type, or a unit trap."""

    code: ClassVar[str] = "InvalidValue"


class NoConversion(PluginError):
    """The provider declares no conversion for the requested value space."""

    code: ClassVar[str] = "NoConversion"


class InvalidRoute(PluginError):
    """A route operation is malformed or names a non-existent slot."""

    code: ClassVar[str] = "InvalidRoute"


class InvalidSourceFamily(PluginError):
    """A provider vocabulary/policy rule rejects the route.

    The POC's single explicit ``force`` escape is **not implemented in V1**: the
    refusal stands and ``force=True`` is itself refused
    (`UnsupportedOverride`), so the error never implies that an override exists.
    """

    code: ClassVar[str] = "InvalidSourceFamily"


class UnsupportedOverride(PluginError):
    """An override/escape argument V1 does not implement was requested.

    An argument that looks like a permission must never be accepted while
    nothing honours it: ``force=True`` refuses **before any host work** with
    this code and zero writes (owner Decision 4, 2026-10-10).
    """

    code: ClassVar[str] = "UnsupportedOverride"


class DependencyViolation(PluginError):
    """An unmet hard dependency rule refuses the mutation before any write."""

    code: ClassVar[str] = "DependencyViolation"


class IncompatibleSettings(PluginError):
    """Provider configuration is invalid (e.g. a missing registry directory)."""

    code: ClassVar[str] = "IncompatibleSettings"


class PlanError(PluginError):
    """The plan cannot be built against the live state."""

    code: ClassVar[str] = "PlanError"


class UnsupportedProvider(PluginError):
    """No applicable provider is configured for the requested target."""

    code: ClassVar[str] = "UnsupportedProvider"


class TargetSliceAmbiguous(PluginError):
    """The state block cannot be uniquely correlated to the target instance."""

    code: ClassVar[str] = "TargetSliceAmbiguous"


# ---------------------------------------------------------------------------
# Conflict / identity
# ---------------------------------------------------------------------------


class TargetChanged(PluginError):
    """The write-target token is stale or mismatched; never re-target silently."""

    code: ClassVar[str] = "TargetChanged"


class IdentityContinuityUnprovable(PluginError):
    """A mutation rested on instance continuity the host cannot prove.

    A caller-supplied write-target token claims *the instance that was validated*.
    The measured host cannot back that claim: removing and re-adding the same
    build at the same slot reproduced the same session handle and the same build
    facts, and no exposed datum was found that separates a replacement from the
    original (P8 `p8_l3_identity__*`). The layer therefore refuses such a mutation
    before any host work instead of accepting a permission it cannot honour (owner
    safety-gate correction, 2026-10-11). Reads keep accepting the token: a read
    makes no write claim.
    """

    code: ClassVar[str] = "IdentityContinuityUnprovable"


class StateConflict(PluginError):
    """The expected state revision disagrees with the live state."""

    code: ClassVar[str] = "StateConflict"


class SnapshotUnavailable(PluginError):
    """A complete, verifiable capture is impossible; the mutation is refused."""

    code: ClassVar[str] = "SnapshotUnavailable"


# ---------------------------------------------------------------------------
# Write outcomes (raised/reported by the transaction)
# ---------------------------------------------------------------------------


class WriteNotApplied(PluginError):
    """A write verb returned success but the read-back shows no effect."""

    code: ClassVar[str] = "WriteNotApplied"


class StateMismatch(PluginError):
    """A state channel read-back does not match the written value."""

    code: ClassVar[str] = "StateMismatch"


class ReadbackMismatch(PluginError):
    """A parameter read-back does not match the written value."""

    code: ClassVar[str] = "ReadbackMismatch"


class WriteOrderViolation(PluginError):
    """A write was attempted in an order the provider forbids."""

    code: ClassVar[str] = "WriteOrderViolation"


class RollbackFailed(PluginError):
    """The restore attempt raised or produced an unverifiable read-back."""

    code: ClassVar[str] = "RollbackFailed"


class RestoreIncomplete(PluginError):
    """Some captured channel was not restored or not compared."""

    code: ClassVar[str] = "RestoreIncomplete"


def _all_subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_all_subclasses(sub))
    return found


ERROR_CODES: tuple[str, ...] = tuple(
    sorted({sub.code for sub in [PluginError, *_all_subclasses(PluginError)]})
)


def error_code(exc: BaseException) -> str | None:
    """The stable code for a plugin error, or ``None`` for foreign exceptions."""
    if isinstance(exc, PluginError):
        return exc.code
    return None


_CODE_TO_ERROR: dict[str, type[PluginError]] = {
    sub.code: sub for sub in [PluginError, *_all_subclasses(PluginError)]
}


def error_from_code(code: str, message: str, detail: dict[str, Any] | None = None) -> PluginError:
    """Rehydrate a typed error from its stable code (problem rows carry codes)."""
    klass = _CODE_TO_ERROR.get(code, PluginError)
    return klass(message, detail)
