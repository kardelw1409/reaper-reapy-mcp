"""The `plugin_*` tools: tables, envelope and registration (contract 06).

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
import time
from typing import Any, Callable, Mapping

from . import logs
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

#: The V1 tool names; disjoint from the existing host tools by prefix.
#: ``plugin_prepare`` (P26) is the universal discovery path: it addresses any FX
#: slot the host reports, with or without a configured provider, and is read-only.
TOOL_NAMES: tuple[str, ...] = (
    "plugin_instances",
    "plugin_describe",
    "plugin_find",
    "plugin_read",
    "plugin_write",
    "plugin_state",
    "plugin_modulation",
    "plugin_prepare",
    "plugin_presets",
    "plugin_wavetable",
    "plugin_sample",
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
        "purpose": (
            "List/resolve plugin instances (candidates, identity, target token); "
            "`scope='project'` lists every loaded FX from the host's own facts, provider or not."
        ),
        "args": {
            "provider": {"type": "string", "required": False, "default": None},
            "selector": {"type": "object", "required": False, "default": None},
            "include_identity": {"type": "boolean", "required": False, "default": True},
            "scope": {
                "type": "string",
                "required": False,
                "default": "provider",
                "enum": ["provider", "project"],
            },
            "include_readiness": {"type": "boolean", "required": False, "default": False},
        },
        "required": (),
        "destructive": False,
        "mutating_ops": (),
        "returns": "instances[], ambiguous, refused[], scope, slots_scanned",
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
        "purpose": (
            "Find WRITABLE/readable control ids of one instance by name, group or module: the "
            "ids plugin_read and plugin_write accept (they are the profile vocabulary, not the "
            "host's display names). Start here when a control id is unknown; for a providerless "
            "FX run plugin_prepare first so an artifact exists to search."
        ),
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
        "purpose": (
            "Batched VALUE READ for named controls of one instance: pass every control id you "
            "need and get the normalized/native/display value per control in one response - "
            "prefer it over one read per control. Control ids are the profile vocabulary "
            "(see plugin_find; for a providerless FX run plugin_prepare first). Reads live "
            "values; use get_fx_param/get_fx_param_list for FX the plugin layer does not cover."
        ),
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
        "purpose": (
            "THE batch write for plugin controls: change several controls (and/or routes) of ONE "
            "FX instance in one request, verified with read-back. Prefer it over repeated single "
            "writes - one request carries the whole set. `request.controls` maps control ids to "
            "values (display units, or a single-value object naming the space explicitly: "
            "{'normalized': 0..1} or {'native': x}); ids come from plugin_find/plugin_read. "
            "Dry-run by default; it applies only with dry_run=false. Refuses the whole set "
            "before writing when any control is "
            "unknown (no partial apply); a failed write is rolled back and reported. "
            "On a providerless FX run plugin_prepare first (its artifact is the addressing basis). "
            "'state'/'routes_*' need a plugin WITH a provider; 'controls' works on both paths. "
            "For a single precise tweak, set_fx_param is the lighter call."
        ),
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "request": {"type": "object", "required": True, "default": None},
            "dry_run": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("instance", "request"),
        "destructive": True,
        "mutating_ops": ("controls", "routes_create", "routes_update", "routes_remove", "state", "wavetable"),
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
        "returns": "state, state_view?, structural_keys[], counts, slice",
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
    "plugin_prepare": {
        "purpose": (
            "Inventory and prepare any FX instance for discovery: read-only host metadata plus a "
            "budgeted display sweep, stored as a reusable prepared artifact. Run it BEFORE "
            "plugin_read/plugin_find/plugin_write on an FX that has no provider (they refuse "
            "without an artifact); mode='reuse' (default) serves the stored artifact and only "
            "measures when none exists, so a repeat call is cheap."
        ),
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "mode": {
                "type": "string",
                "required": False,
                "default": "reuse",
                "note": (
                    "reuse serves a stored prepared artifact (and measures the metadata only if "
                    "none exists); inventory reads metadata only; prepare adds the display sweep; "
                    "deep adds adaptive refinement of disputed boundaries. A stored artifact "
                    "answers only a request whose mode it matches or outranks; a stronger "
                    "request re-measures and reports the replaced artifact"
                ),
            },
            "sample_indices": {"type": "array", "required": False, "default": None},
            "max_controls": {"type": "integer", "required": False, "default": None},
            "grid_points": {"type": "integer", "required": False, "default": 17},
            "refine_budget": {"type": "integer", "required": False, "default": None},
            "force": {
                "type": "boolean",
                "required": False,
                "default": False,
                "note": "ignore a stored artifact and measure again",
            },
            "include_controls": {"type": "boolean", "required": False, "default": False},
        },
        "required": ("instance",),
        "destructive": False,
        "mutating_ops": (),
        "returns": (
            "identity, prepared{status,path,mode,...}, modules[], module_signals[], classes{}, "
            "counts{}, unknown_by_reason{}, cost{}, warnings[]"
        ),
    },
    "plugin_presets": {
        "purpose": (
            "The plugin's OWN preset library - its built-in preset browser's files, not the "
            "host's session presets (the plugin does not expose those). op='search' lists "
            "presets with stable ids; op='info' shows one preset's metadata; op='apply' loads "
            "ONE preset into the plugin (dry-run by default; a real apply is a CHAIN of verified "
            "writes - measured 2026-10-17: ~19 s for a 3-unit apply and ~1.5 min for a 38-unit "
            "one on a 2858-parameter instance, where every unit pays a base read, the write, a "
            "read-back and a full parameter re-read); op='save' writes the live "
            "plugin state to a new preset file (the file name gets a numeric suffix when it "
            "already exists). An apply that cannot carry every part still commits and says so: "
            "`partial: true` plus a named gap per part (a routing the running build drops, a "
            "table or sample that would grow the engine, a value a hard rule guards). Providers "
            "without a preset capability refuse explicitly."
        ),
        "args": {
            "instance": {"type": "object", "required": True, "default": None},
            "op": {
                "type": "string",
                "required": True,
                "default": None,
                "enum": ["search", "info", "apply", "save", "init"],
            },
            "query": {
                "type": "string",
                "required": False,
                "default": None,
                "note": (
                    "search: whitespace-separated tokens matched against name, embedded name, author "
                    "and id, RANKED (whole word beats word prefix beats substring). Each row says "
                    "which fields scored (`matched_in`) and a token that hit nothing is reported "
                    "in `unmatched_terms` - so a few rows are usually enough"
                ),
            },
            "style": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "search: exact style filter (the preset's own `preset_style`)",
            },
            "author": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "search: substring filter on the author/pack name",
            },
            "preset": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "info/apply: the preset id from search (its path relative to the library root)",
            },
            "limit": {"type": "integer", "required": False, "default": 20},
            "offset": {"type": "integer", "required": False, "default": 0},
            "name": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "save: the new preset's name (file stem); required for save",
            },
            "bank": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "save: optional single folder under the user preset root",
            },
            "dry_run": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("instance", "op"),
        "destructive": True,
        "mutating_ops": ("apply", "save", "init"),
        "returns": (
            "op, preset(s), counts, gaps[], plan/outcome for apply, file info for save; "
            "`partial` and `dropped_routes` name the routings the plugin itself discarded"
        ),
    },
    "plugin_wavetable": {
        "purpose": (
            "SOUND DESIGN: the waveform an oscillator reads, described in one small object and "
            "written into a wavetable slot. op='write' builds a table from a spec and delivers it "
            "through the same verified state channel as any other write (dry-run by default); "
            "op='preview' renders the spec host-free (per-frame peak/RMS/partials, plus the frame "
            "count and the state cost); op='read' reports what the addressed slot HOLDS now "
            "(source type, frames, table_id, and the same per-frame numbers) - a SINGLE-frame table "
            "is what makes `Oscillator N Wave Frame` inert, because there is no second frame to "
            "move to, so a write or a modulation route to the frame returns the table's own frame. "
            "THE SPEC: exactly one of `shape` (one name or a list for a morph), `harmonics` "
            "([[partial, amplitude], ...] or one such list per frame) or `samples` (raw, "
            "resampled to the plugin's frame width); optional `frames` (1..256, default = the "
            "number of items listed, interpolated when larger), `duty` (a pulse's width as a "
            "FRACTION of the cycle, 0..1 - pass 0.25 for 25 percent, not 25), `phase` "
            "(degrees), `name`, `full_normalize`, `remove_all_dc`. The response carries `shapes` "
            "so the vocabulary is discoverable without guessing, and every refusal names what it "
            "accepted."
        ),
        "args": {
            "instance": {"type": "object", "required": False, "default": None},
            "op": {
                "type": "string",
                "required": True,
                "default": None,
                "enum": ["write", "read", "preview"],
            },
            "oscillator": {
                "type": "integer",
                "required": False,
                "default": 1,
                "note": "which oscillator's table slot (1..3)",
            },
            "table": {
                "type": "object",
                "required": False,
                "default": None,
                "note": "write/preview: the spec described above",
            },
            "frames": {
                "type": "integer",
                "required": False,
                "default": 2,
                "note": "preview/read: how many frames of the morph to describe (first and last)",
            },
            "points": {
                "type": "integer",
                "required": False,
                "default": 8,
                "note": "preview/read: how many sample values per described frame",
            },
            "dry_run": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("op",),
        "destructive": True,
        "mutating_ops": ("write",),
        "returns": (
            "op, oscillator, table_id/described frames for write+read, preview{described, frames[]} "
            "for preview, gaps[] and outcome for a write"
        ),
    },
    "plugin_sample": {
        "purpose": (
            "THE SAMPLER (the plugin's sample engine, an extra oscillator): put a waveform in and "
            "switch it on. op='load' takes `file_path` (a PCM WAV) or `samples` (raw int16 values "
            "+ `sample_rate`) and writes the plugin's sample BLOCK through the same verified "
            "state channel as every other write (landing bound, sample balloon, read-back, "
            "rollback); the sampler's controls ride the SAME batch, so `on` (default true), "
            "`loop`, `destination`, `level`, `transpose`, `tune`, `pan`, `keytrack` address them "
            "by name. op='read' reports what the sampler holds now (name, length, sample_rate, "
            "channels, seconds, payload bytes) and its controls' current DISPLAY values "
            "(`{control_id: value}` - the same strings plugin_write accepts; call plugin_read for "
            "the profile's full record, with normalized value, unit and law). The "
            "sampler is an ORDINARY state block: it carries the PCM inline, and its controls are "
            "ordinary parameters. The "
            "payload is base64 int16 PCM inline in the state: `samples` is the LEFT channel and "
            "`samples_stereo` the RIGHT, and the plugin's own buffer bound is 1,764,000 samples "
            "(a longer file is refused by name, not truncated)."
        ),
        "args": {
            "instance": {"type": "object", "required": False, "default": None},
            "op": {
                "type": "string",
                "required": True,
                "default": None,
                "enum": ["read", "load"],
            },
            "file_path": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "load: a PCM WAV (8/16/32-bit); its own rate and channel count are used",
            },
            "samples": {
                "type": "array",
                "required": False,
                "default": None,
                "note": (
                    "load: raw int16 values (one row = mono, or [left, right]); needs `sample_rate`"
                ),
            },
            "sample_rate": {
                "type": "integer",
                "required": False,
                "default": None,
                "note": "load: the rate the buffer plays at (required with raw `samples`)",
            },
            "name": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "load: the name the plugin shows (defaults to the file stem)",
            },
            "on": {
                "type": "boolean",
                "required": False,
                "default": True,
                "note": "load: switch the sampler on (default true - a load that stays silent is a bug)",
            },
            "loop": {"type": "boolean", "required": False, "default": None},
            "destination": {
                "type": "string",
                "required": False,
                "default": None,
                "note": "one of the control's own members, e.g. 'DIRECT OUT' or 'EFFECTS'",
            },
            "level": {"type": "number", "required": False, "default": None},
            "transpose": {"type": "number", "required": False, "default": None},
            "tune": {"type": "number", "required": False, "default": None},
            "pan": {"type": "number", "required": False, "default": None},
            "keytrack": {"type": "boolean", "required": False, "default": None},
            "dry_run": {"type": "boolean", "required": False, "default": True},
        },
        "required": ("op",),
        "destructive": True,
        "mutating_ops": ("load",),
        "returns": (
            "op, path, described (name/length/channels/payload), controls sent, controls_skipped, "
            "outcome; op='read' adds the sampler's current control values"
        ),
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


def _argument_shape(name: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """A call's arguments as SHAPE, never content: sizes, keys and scalars only.

    A tool argument can carry a whole document or a control set; copying it into a log
    would put the user's material in a diagnostic file and could dwarf the log itself.
    Scalars (a preset id, an op, a flag, a selector) are small and are what a reader
    needs to tell two calls apart, so they are kept verbatim.
    """
    shape: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            text = value if isinstance(value, bool) or not isinstance(value, str) else value
            if isinstance(text, str) and len(text) > 120:
                shape[key] = f"<str {len(text)} chars>"
            else:
                shape[key] = text
        elif isinstance(value, Mapping):
            shape[key] = f"<object {len(value)} keys: {','.join(sorted(str(k) for k in value)[:8])}>"
        elif isinstance(value, (list, tuple)):
            shape[key] = f"<list {len(value)} items>"
        else:
            shape[key] = f"<{type(value).__name__}>"
    return shape


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

        def logged(**kwargs: Any) -> dict[str, Any]:
            """The tool the transport actually sees: same answer, one log line around it.

            Owner's ask (2026-10-17): a call that takes minutes must be visible WHILE it
            runs. The line is written when the call ends (duration included), the
            per-unit progress comes from the transaction itself (see
            `plugins/logs.py`), and neither can turn a working call into an error. The
            argument shape is logged by KEY and SIZE - a tool argument can carry a
            whole document, and a log is not a place to copy the user's material.
            """
            started = time.perf_counter()
            answer = tool(**kwargs)
            try:
                logs.event(
                    "tool",
                    tool=name,
                    ok=bool(answer.get("ok")),
                    seconds=round(time.perf_counter() - started, 3),
                    args=_argument_shape(name, kwargs),
                    error=(answer.get("error") or {}).get("code"),
                )
            except Exception:  # noqa: BLE001 - logging never fails a call
                pass
            return answer

        logged.__name__ = name
        logged.__doc__ = synthesize_docstring(name)
        # A `**kwargs`-only function is modelled by FastMCP as ONE required field
        # called `kwargs`, so a real MCP client could never call the tool (found by
        # the P9 tests, which drive `mcp.call_tool` rather than the closure). The
        # declared signature gives the server the tool's real argument names; the
        # closure still performs its own required-argument validation, so a missing
        # argument is an envelope refusal rather than a validation error.
        logged.__signature__ = _declared_signature(spec)
        mcp.tool(name=name)(logged)

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
