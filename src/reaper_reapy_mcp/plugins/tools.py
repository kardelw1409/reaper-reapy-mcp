"""The 7 `plugin_*` tools: tables, envelope and registration (contract 06).

`TOOL_NAMES` / `SPEC` / `DISPATCH` tables, the schema-2 envelope builders,
synthesized docstrings and the registration closure. The bodies themselves live
in `api.py` (P9) and are attached to `DISPATCH` by `setup_plugin_tools`, which is
what `create_server()` calls. Registration is unconditional (AUD-013) and does
no host work at import/registration time.

Envelope rules (06 §2): a typed `error` is never text inside a success payload;
`outcome` accompanies every mutating call and every failed post-mutation call;
"zero host writes" may only be claimed from `outcome.host.write_calls_attempted
== 0` - the envelope builder never invents counters.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from .errors import InvalidValue, PluginError

__all__ = [
    "SCHEMA_VERSION",
    "TOOL_NAMES",
    "SPEC",
    "DISPATCH",
    "register_dispatch",
    "ok_envelope",
    "error_envelope",
    "synthesize_docstring",
    "setup_plugin_tools",
    "INTERNAL_ERROR_CODE",
]

#: Envelope schema version (06 §2; the POC envelope was schema 1).
SCHEMA_VERSION = 2

#: The seven V1 tool names; disjoint from the existing host tools by prefix.
TOOL_NAMES: tuple[str, ...] = (
    "plugin_instances",
    "plugin_describe",
    "plugin_find",
    "plugin_read",
    "plugin_write",
    "plugin_state",
    "plugin_modulation",
)

#: Tool-layer fault classification for an unexpected internal exception. This
#: is an envelope-level fault code, not a plugin-domain error: the closed
#: domain taxonomy lives in errors.py (05 §7).
INTERNAL_ERROR_CODE = "InternalError"

#: Declared specs. `destructive` marks a tool whose operations can change the
#: plugin state; every destructive tool MUST declare `dry_run` with default
#: True (AUD-009) - enforced by the mutation-default audit test.
SPEC: dict[str, dict[str, Any]] = {
    "plugin_instances": {
        "purpose": "List/resolve plugin instances (candidates, identity, target token).",
        "args": {
            "provider": {"type": "string", "required": False, "default": None},
            "selector": {"type": "object", "required": False, "default": None},
            "include_identity": {"type": "boolean", "required": False, "default": True},
        },
        "required": (),
        "destructive": False,
        "mutating_ops": (),
        "returns": "instances[], ambiguous, refused[]",
    },
    "plugin_describe": {
        "purpose": "Identity, module summary and capability counts for one instance.",
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "detail": {"type": "string", "required": False, "default": "summary"},
        },
        "required": ("instance",),
        "destructive": False,
        "mutating_ops": (),
        "returns": "identity, profile, modules[], counts, routes, warnings[]",
    },
    "plugin_find": {
        "purpose": "Search controls/modules by name or group; detail for one group.",
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "query": {"type": "string", "required": False, "default": None},
            "group": {"type": "string", "required": False, "default": None},
            "kind": {"type": "string", "required": False, "default": "control"},
            "limit": {"type": "integer", "required": False, "default": 12},
            "include_capability": {"type": "boolean", "required": False, "default": False},
        },
        "required": ("instance",),
        "destructive": False,
        "mutating_ops": (),
        "returns": "matches[], suggestions[], groups[]",
    },
    "plugin_read": {
        "purpose": "Targeted, batched current-value reads for named controls.",
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "controls": {"type": "array", "required": True, "default": None},
            "include_display": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("instance", "controls"),
        "destructive": False,
        "mutating_ops": (),
        "returns": "values{}, problems[]",
    },
    "plugin_write": {
        "purpose": "Batch-first verified plugin write; dry-run by default, explicit apply required.",
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "request": {"type": "object", "required": True, "default": None},
            "dry_run": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("instance", "request"),
        "destructive": True,
        "mutating_ops": ("controls", "routes_create", "routes_update", "routes_remove", "state"),
        "returns": "committed, plan_summary, outcome",
    },
    "plugin_state": {
        "purpose": "State/document inspection for the selected instance (inspect only in V1).",
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "op": {"type": "string", "required": False, "default": "inspect"},
            "include": {"type": "array", "required": False, "default": None},
        },
        "required": ("instance",),
        "destructive": False,
        "mutating_ops": (),
        "returns": "state, structural_keys[], counts, slice",
    },
    "plugin_modulation": {
        "purpose": "Route lifecycle (list/create/update/remove) and semantic depth conversion.",
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "op": {"type": "string", "required": True, "default": None},
            "routes": {"type": "array", "required": False, "default": None},
            "selector": {"type": "object", "required": False, "default": None},
            "destination": {"type": "string", "required": False, "default": None},
            "depth": {"type": "number", "required": False, "default": None},
            "force": {
                "type": "boolean",
                "required": False,
                "default": False,
                "note": (
                    "not supported: V1 implements no override, so force=true is refused "
                    "(UnsupportedOverride) before any host contact (owner Decision 4)"
                ),
            },
            "dry_run": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("instance", "op"),
        "destructive": True,
        "mutating_ops": ("create", "update", "remove"),
        "returns": "routes[] | per-route outcome | depth result; outcome for mutations",
    },
}

#: name -> body. Bodies are added by the phases that implement them; the table
#: is consulted at call time so registration never depends on their presence.
DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {}


def register_dispatch(name: str, fn: Callable[..., dict[str, Any]]) -> None:
    """Attach a tool body. Unknown names are refused loudly (a typo must not
    silently create a tool that can never be dispatched)."""
    if name not in TOOL_NAMES:
        raise InvalidValue(
            f"{name!r} is not a declared plugin tool",
            detail={"tool_names": list(TOOL_NAMES)},
        )
    DISPATCH[name] = fn


# ---------------------------------------------------------------------------
# Envelope (schema 2)
# ---------------------------------------------------------------------------


def ok_envelope(
    tool: str,
    data: Any,
    *,
    warnings: list[dict[str, Any]] | None = None,
    outcome: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "tool": tool,
        "schema": SCHEMA_VERSION,
        "ok": True,
        "data": data,
        "warnings": list(warnings or []),
    }
    if outcome is not None:
        envelope["outcome"] = outcome
    if meta is not None:
        envelope["meta"] = meta
    return envelope


def error_envelope(
    tool: str,
    error: PluginError | dict[str, Any],
    *,
    warnings: list[dict[str, Any]] | None = None,
    outcome: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Failure envelope. A failed post-mutation call carries BOTH the typed
    error and the complete outcome (05 §2.8); this builder only refuses to
    invent one - the caller supplies it or it is absent."""
    typed = error.to_dict() if isinstance(error, PluginError) else dict(error)
    envelope: dict[str, Any] = {
        "tool": tool,
        "schema": SCHEMA_VERSION,
        "ok": False,
        "error": typed,
        "warnings": list(warnings or []),
    }
    if outcome is not None:
        envelope["outcome"] = outcome
    if meta is not None:
        envelope["meta"] = meta
    return envelope


def identity_meta() -> dict[str, Any]:
    """The mechanism-level identity disclosure, safe to attach to any envelope.

    A refusal raised before an instance is resolved has no target token to report,
    but it must not leave a caller guessing what the layer can and cannot prove
    about identity, so the disclosure travels with the refusal too (Decision 3).
    """
    from .resolve import identity_basis  # noqa: PLC0415 - keeps this module import-light

    return {"identity_basis": identity_basis()}


def synthesize_docstring(name: str) -> str:
    spec = SPEC[name]
    return (
        f"{spec['purpose']}\n\n"
        f"args: {json.dumps(spec['args'])}\n"
        f"destructive: {spec['destructive']}\n"
        f"returns: {spec['returns']}\n"
        "Returns a schema-2 envelope: on failure `ok` is false and `error` is a "
        "typed {code, message, detail} object - never text inside a success "
        "payload; mutating calls carry an `outcome` block whose counters say "
        "exactly what was written and verified."
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def setup_plugin_tools(mcp: Any, *, config: Any = None) -> None:
    """Register the 7 plugin tools on the existing FastMCP server.

    Unconditional (AUD-013): registration never depends on provider discovery
    or configuration, performs no host call and imports no host module. The
    closure follows the proven SPEC-driven pattern: required-argument
    validation produces an envelope refusal (never a traceback), a `TypeError`
    from dispatch is reported with the accepted argument list, and a body
    returning an envelope is passed through untouched.

    The production bodies (P9) are attached here as well. They are imported at
    this point, not at module import: registering the tools must stay free of
    provider work, and the bodies resolve their provider from the configuration
    on **call**. With no provider configured every tool answers a typed
    `UnsupportedProvider` - visible and inert, never missing.
    """
    from . import api as _api  # noqa: PLC0415 - lazy: keeps registration side-effect free

    _api.register_production_bodies()

    def _register(name: str) -> None:
        spec = SPEC[name]

        def tool(**kwargs: Any) -> dict[str, Any]:
            missing = [arg for arg in spec["required"] if kwargs.get(arg) is None]
            if missing:
                return error_envelope(
                    name,
                    InvalidValue(
                        f"missing required argument(s): {', '.join(missing)}",
                        detail={"required": list(spec["required"]), "accepted": list(spec["args"])},
                    ),
                    meta=identity_meta(),
                )
            # The transport fills every declared argument, with `None` for one the
            # caller omitted, and a direct call may omit arguments entirely. Either
            # way the SPEC default applies, so a body always sees the declared
            # default explicitly (found by the P9 tests: `dry_run` defaulted to
            # `False`). A supplied `False`/`0` is kept - only `None` means "not
            # supplied".
            arguments = {
                arg: (spec["args"][arg].get("default") if kwargs.get(arg) is None else kwargs[arg])
                for arg in spec["args"]
            }
            fn = DISPATCH.get(name)
            if fn is None:
                return error_envelope(
                    name,
                    {
                        "code": INTERNAL_ERROR_CODE,
                        "message": f"no dispatch body registered for {name!r}",
                        "detail": {"accepted": list(spec["args"])},
                    },
                    meta=identity_meta(),
                )
            try:
                return fn(config=config, **arguments)
            except PluginError as exc:
                return error_envelope(name, exc, meta=identity_meta())
            except TypeError as exc:
                return error_envelope(
                    name,
                    InvalidValue(
                        f"bad arguments: {exc}",
                        detail={"accepted": list(spec["args"]), "required": list(spec["required"])},
                    ),
                    meta=identity_meta(),
                )
            except Exception as exc:  # noqa: BLE001 - a tool must answer an envelope
                return error_envelope(
                    name,
                    {
                        "code": INTERNAL_ERROR_CODE,
                        "message": f"{type(exc).__name__}: {exc}",
                        "detail": {},
                    },
                    meta=identity_meta(),
                )

        tool.__name__ = name
        tool.__doc__ = synthesize_docstring(name)
        # A `**kwargs`-only function is modelled by FastMCP as ONE required field
        # called `kwargs`, so a real MCP client could never call the tool (found by
        # the P9 tests, which drive `mcp.call_tool` rather than the closure). The
        # declared signature gives the server the tool's real argument names; the
        # closure still performs its own required-argument validation, so a missing
        # argument is an envelope refusal rather than a validation error.
        tool.__signature__ = _declared_signature(spec)
        mcp.tool(name=name)(tool)

    for name in TOOL_NAMES:
        _register(name)


def _declared_signature(spec: dict[str, Any]) -> Any:
    """A keyword-only signature over the tool's declared arguments.

    Every argument is optional **at the transport level**: the SPEC's `required`
    list is enforced by the closure, which answers an envelope naming what is
    missing (06 §2). Enforcement at the transport level would raise a protocol
    error instead, which is exactly what the envelope contract forbids.
    """
    parameters = [
        inspect.Parameter(str(arg), inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any)
        for arg in spec["args"]
    ]
    return inspect.Signature(parameters, return_annotation=dict)
