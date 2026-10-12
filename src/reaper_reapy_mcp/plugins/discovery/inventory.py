"""Host inventory: everything the host exposes about an FX's parameters (P26 §6).

The collector's job is to try **every** channel and record what each one
answered, including "the plugin has no such thing" and "this host build has no
such function". A channel that failed is a row, not an exception, and a channel
that was never available is never reported as an empty result.

Two read shapes are supported, and the collector prefers the batched one:

``param_metadata``
    one batched hop returning name, ident, current value, step sizes, formatted
    current text and named-config entries per parameter (the P26 bridge verb);
``param_names`` / ``param_idents`` / ``read_params``
    the P1-P25 verbs. Used when the batched verb is absent, so a bridge that has
    not been extended still produces a (smaller) inventory instead of failing -
    and the artifact says which channels were not attempted.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .types import Inventory, ParamRow, StepSizes

__all__ = [
    "DEFAULT_CHANNELS",
    "collect_inventory",
    "inventory_from_records",
    "facts_identity",
    "facts_location",
]

#: Channels the collector attempts, in order.
DEFAULT_CHANNELS: tuple[str, ...] = ("name", "ident", "value", "steps", "formatted", "named", "section")

#: Metadata channels the format adapters could add (recorded as unavailable rows
#: when no adapter provides them).
NATIVE_CHANNELS = ("native_range", "native_step", "native_default", "native_units", "native_enum", "native_group")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def facts_identity(facts: Any) -> dict[str, Any]:
    """The identity block the inventory records (host facts, nothing inferred)."""
    return {
        "fx_name": _get(facts, "fx_name"),
        "class_id": _get(facts, "class_id"),
        "module": _get(facts, "module"),
        "format": _get(facts, "format"),
        "version": _get(facts, "version"),
        "param_count": _get(facts, "param_count"),
        "preset": _get(facts, "preset"),
    }


def facts_location(facts: Any) -> dict[str, Any]:
    return {
        "track_index": _get(facts, "track_index"),
        "fx_index": _get(facts, "fx_index"),
        "guid": _get(facts, "fx_guid") or _get(facts, "guid"),
    }


def _channel_result(bridge: Any, verb: str, *args: Any, **kwargs: Any) -> tuple[str, Any, str | None]:
    """Call one bridge verb, classifying absence instead of raising."""
    method = getattr(bridge, verb, None)
    if not callable(method):
        return "unsupported", None, f"the bridge has no {verb} verb"
    try:
        return "ok", method(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001 - a channel failure is a row
        return "error", None, f"{type(exc).__name__}: {exc}"


def collect_inventory(
    bridge: Any,
    ref: Any,
    facts: Any,
    *,
    indices: Sequence[int] | None = None,
    channels: Sequence[str] = DEFAULT_CHANNELS,
    batch_size: int = 2048,
    chunk_size: int = 256,
) -> Inventory:
    """Build the inventory of one FX instance.

    The metadata read is **chunked**: a window of parameters per host call, with
    the rows of every completed window kept. Measured live (P26), an unheld
    2858-parameter read wedged at the 60 s timeout and reported nothing, so a single
    batched call for a whole instance is not merely slow - it loses the inventory
    when it happens. A window that fails is recorded as its own row and the rest of
    the parameters are still read.
    """
    identity = facts_identity(facts)
    reported = _get(facts, "param_count")
    try:
        count = int(reported) if reported is not None else 0
    except (TypeError, ValueError):
        count = 0
    inventory = Inventory(
        identity=identity,
        param_count=count,
        reported_param_count=int(reported) if isinstance(reported, (int, float)) else None,
        origin="host",
    )
    wanted = list(indices) if indices is not None else list(range(count))
    if count and wanted and max(wanted) >= count:
        inventory.notes.append(
            f"{sum(1 for index in wanted if index >= count)} requested index(es) are outside the "
            f"host's reported parameter count ({count})"
        )
        wanted = [index for index in wanted if 0 <= index < count]
    rows: dict[int, ParamRow] = {index: ParamRow(index=index) for index in wanted}
    window = max(1, int(chunk_size))
    windows = [wanted[start : start + window] for start in range(0, len(wanted), window)] or [[]]

    batched_calls = 0
    failed_windows: list[dict[str, Any]] = []
    used_batched = False
    legacy_messages: list[str] = []
    for offset, block in enumerate(windows):
        status, payload, error = _channel_result(
            bridge, "param_metadata", ref, block, include=tuple(channels)
        )
        if status == "ok" and isinstance(payload, Mapping):
            used_batched = True
            _apply_batched(
                inventory=inventory,
                rows=rows,
                payload=payload,
                channels=channels,
                wanted=block,
                default_batch=batch_size,
            )
            batched_calls += 1
            continue
        if status == "unsupported":
            legacy_messages.append(str(error or "the batched verb is unavailable"))
            break
        failed_windows.append(
            {
                "window": offset,
                "from": block[0] if block else None,
                "to": block[-1] if block else None,
                "status": status,
                "message": error,
            }
        )
        for index in block:
            row = rows.get(index)
            if row is None:
                continue
            for channel in channels:
                row.attempt(channel, status, message=error)
    inventory.attempt(
        "param_metadata",
        "ok" if used_batched and not failed_windows else ("unsupported" if not used_batched else "error"),
        calls=batched_calls,
        message=(failed_windows[0]["message"] if failed_windows else None),
        channels=list(channels),
        windows=len(windows),
        failed_windows=failed_windows[:8],
    )
    if not used_batched and len(legacy_messages) == len(windows) == 1 and legacy_messages:
        _collect_legacy(inventory=inventory, bridge=bridge, ref=ref, rows=rows, wanted=wanted, channels=channels)
    if failed_windows:
        inventory.notes.append(
            f"{len(failed_windows)} of {len(windows)} metadata window(s) failed; their parameters keep the "
            "attempt status of the failing window and were not silently dropped"
        )

    inventory.params = [rows[index] for index in wanted]
    if not inventory.params:
        inventory.notes.append("the host reported no parameters for this instance")
    return inventory


def _apply_batched(
    *,
    inventory: Inventory,
    rows: dict[int, ParamRow],
    payload: Mapping[str, Any],
    channels: Sequence[str],
    wanted: Sequence[int],
    default_batch: int,
) -> None:
    channel_status = payload.get("channels") or {}
    if isinstance(channel_status, Mapping):
        for name, value in channel_status.items():
            if value != "ok":
                inventory.attempt(f"channel:{name}", str(value), calls=0)
    row_list = payload.get("rows") or []
    seen: set[int] = set()
    for raw in row_list:
        if not isinstance(raw, Mapping):
            continue
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            continue
        row = rows.get(index)
        if row is None:
            row = ParamRow(index=index)
            rows[index] = row
        seen.add(index)
        _apply_row(row, raw)
    for index in wanted:
        row = rows.get(index)
        if row is None:
            continue
        for channel in channels:
            row.attempt(channel, "ok" if _channel_present(row, channel) else "absent")
        if index not in seen and not row.attempts:
            row.attempt("param_metadata", "absent", message="the batch returned no row for this parameter")


def _channel_present(row: ParamRow, channel: str) -> bool:
    if channel == "name":
        return bool(row.name)
    if channel == "ident":
        return bool(row.ident)
    if channel == "value":
        return row.current_normalized is not None
    if channel == "steps":
        return row.steps.reported or row.steps.is_toggle or row.steps.status == "ok"
    if channel == "formatted":
        return "formatted" in row.extra
    if channel == "named":
        return bool(row.named_config)
    if channel == "section":
        return row.section is not None
    if channel.startswith("native_"):
        return channel in row.extra
    return channel in row.extra


def _apply_row(row: ParamRow, raw: Mapping[str, Any]) -> None:
    if raw.get("name"):
        row.name = str(raw["name"])
    if raw.get("ident"):
        row.ident = str(raw["ident"])
    value = raw.get("value")
    if isinstance(value, (int, float)):
        row.current_normalized = float(value)
    steps = raw.get("steps")
    if isinstance(steps, Mapping):
        row.steps = StepSizes(
            reported=bool(steps.get("reported")),
            step=float(steps.get("step") or 0.0),
            small=float(steps.get("small") or 0.0),
            large=float(steps.get("large") or 0.0),
            is_toggle=bool(steps.get("is_toggle")),
            status=str(steps.get("status") or ("ok" if steps.get("reported") else "absent")),
            note=(str(steps["note"]) if steps.get("note") else None),
        )
    elif isinstance(steps, (list, tuple)):
        row.steps = _steps_from_tuple(steps)
    if raw.get("formatted") is not None:
        row.extra["formatted"] = str(raw.get("formatted"))
    named = raw.get("named_config") or raw.get("named")
    if isinstance(named, Mapping) and named:
        row.named_config = dict(named)
        # A named-config channel that answered with values exists; the default
        # value is the one key the P22 round measured a plugin can decline.
        if "default_value" in row.named_config:
            row.extra["native_default"] = row.named_config["default_value"]
    if raw.get("section") is not None:
        row.section = str(raw.get("section"))
    if isinstance(raw.get("native_range"), (list, tuple)) and len(raw["native_range"]) == 2:
        try:
            row.native_range = (float(raw["native_range"][0]), float(raw["native_range"][1]))
        except (TypeError, ValueError):
            row.native_range = None
    for key in ("native_step", "native_units", "native_enum", "native_group"):
        if raw.get(key) is not None:
            row.extra[key] = raw[key]
    extra = raw.get("extra")
    if isinstance(extra, Mapping):
        row.extra.update(dict(extra))


def _steps_from_tuple(steps: Sequence[Any]) -> StepSizes:
    values = list(steps) + [0.0, 0.0, 0.0, 0]
    try:
        step = float(values[0] or 0.0)
        small = float(values[1] or 0.0)
        large = float(values[2] or 0.0)
        toggle = bool(values[3])
    except (TypeError, ValueError):
        return StepSizes(status="error", note="the host's step tuple was not numeric")
    reported = step > 0.0 or small > 0.0 or large > 0.0
    return StepSizes(
        reported=reported,
        step=step,
        small=small,
        large=large,
        is_toggle=toggle,
        status="ok" if reported or toggle else "absent",
        note=None if reported or toggle else "the host reports no step for this parameter",
    )


def _collect_legacy(
    *,
    inventory: Inventory,
    bridge: Any,
    ref: Any,
    rows: dict[int, ParamRow],
    wanted: Sequence[int],
    channels: Sequence[str],
) -> None:
    """The pre-P26 verb set: three batched reads, the rest recorded as unattempted."""
    legacy = {
        "name": ("param_names", lambda payload, index: setattr(rows[index], "name", payload)),
        "ident": ("param_idents", lambda payload, index: setattr(rows[index], "ident", payload)),
        "value": (
            "read_params",
            lambda payload, index: setattr(rows[index], "current_normalized", payload),
        ),
    }
    for channel, (verb, apply) in legacy.items():
        if channel not in channels:
            continue
        status, payload, error = _channel_result(bridge, verb, ref, list(wanted))
        inventory.attempt(verb, status, calls=1 if status == "ok" else 0, message=error)
        if status != "ok" or not isinstance(payload, (list, tuple)):
            if status == "ok":
                inventory.attempt(verb, "error", calls=0, message="the verb returned no list")
            for index in wanted:
                rows[index].attempt(channel, "error" if status == "error" else "unsupported", message=error)
            continue
        for offset, index in enumerate(wanted):
            value = payload[offset] if offset < len(payload) else None
            if value is None:
                rows[index].attempt(channel, "absent", message="the host returned no value")
                continue
            try:
                apply(value, index)
            except (TypeError, ValueError):
                rows[index].attempt(channel, "absent", message="the host's value was not usable")
                continue
            rows[index].attempt(channel, "ok")
    for channel in channels:
        if channel in legacy:
            continue
        for index in wanted:
            rows[index].attempt(channel, "not_attempted", message="the batched metadata verb is not available")


def inventory_from_records(
    records: Iterable[Mapping[str, Any]],
    *,
    identity: Mapping[str, Any] | None = None,
    param_count: int | None = None,
) -> Inventory:
    """Build an inventory from recorded rows (the offline replay path).

    The rows are the same shape a live run writes, so a replay exercises the same
    analyzers; the inventory records ``origin="recorded"`` so no artifact can be
    mistaken for a live measurement.
    """
    rows: list[ParamRow] = []
    for raw in records:
        row = ParamRow(index=int(raw.get("index", len(rows))))
        _apply_row(row, raw)
        attempts = raw.get("attempts")
        if isinstance(attempts, Mapping):
            for name, entry in attempts.items():
                if isinstance(entry, Mapping):
                    row.attempts[str(name)] = _attempt_from_row(str(name), entry)
        rows.append(row)
    count = int(param_count) if param_count is not None else len(rows)
    inventory = Inventory(
        identity=dict(identity or {}),
        param_count=count,
        reported_param_count=count,
        params=rows,
        origin="recorded",
    )
    inventory.attempt("param_metadata", "ok", calls=0, message="replayed from a recorded artifact")
    return inventory


def _attempt_from_row(source: str, raw: Mapping[str, Any]) -> Any:
    from .types import Attempt

    return Attempt(
        source=source,
        status=str(raw.get("status") or "not_attempted"),
        calls=int(raw.get("calls") or 0),
        message=(str(raw["message"]) if raw.get("message") else None),
        detail=dict(raw.get("detail") or {}),
    )
