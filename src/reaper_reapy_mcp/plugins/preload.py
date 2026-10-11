"""Startup preload of prepared capability profiles (09 §4-§6; owner Decision 4).

The runtime question this module answers: *"the profiles were prepared once -
are they loaded when the server starts, instead of being researched again on the
first request?"*

What runs here, at server start:

1. resolve the configured providers (``REAPER_MCP_PROVIDERS``) and their **key
   material** - the declared revision, the digest over their behaviour-affecting
   source files and the digest over the data sources they would read. That is
   the same material the cache key is made of, and it is computed **without
   building a profile and without touching the host**;
2. scan the cache directory for ``profile_*.json`` entries, and for each one
   check the *identity-independent* part of the validation: the cache format
   key, the profile schema version, the provider id, the entry's own key
   self-derivation (its provenance must re-derive the ``cache_key`` it is filed
   under, using its own embedded fingerprint) and - only for entries whose
   declared provenance matches the live key material, because nothing else can
   ever be served - the content hash, the count cross-check and the profile
   validation table;
3. keep the entries that pass in an in-memory index, and report what was
   scanned, prepared and skipped, with a reason per skip.

What deliberately does **not** happen here:

- no host contact, no connection, no enumeration of plugin instances - the live
  fingerprint (and therefore the final cache key) is only knowable when a tool
  call resolves an instance, and that is where the remaining checks run;
- no build of an unprepared plugin's profile (the expensive step stays on the
  first controlled call, or in a separate preparation procedure);
- no writes, no quarantine, no deletion: an entry that fails a check is skipped
  and left alone, and the runtime path (``ProfileCache.load``) is what reports
  and quarantines it when that key is actually addressed - startup must not
  mutate user data as a side effect of starting;
- no failure ever escapes: a broken provider or an unreadable directory becomes
  a row in the report, never an exception into ``create_server``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .cache import (
    compute_cache_key,
    key_material_of,
    profile_content_hash,
    profile_counts,
    cache_directory,
)
from .errors import PluginError
from .profile import (
    CACHE_FORMAT_KEY,
    PROFILE_SCHEMA_VERSION,
    ProfileValidation,
    validate_profile,
)

__all__ = [
    "PreparedEntry",
    "PreloadReport",
    "PreparedProfiles",
    "prepared_profiles",
    "prepared_lookup",
    "preload_from_env",
    "framing_identity_of",
    "entry_self_key",
]

#: Identity fields the profile embeds (its own fingerprint's shape). A prepared
#: entry's key is derived from these, never from live facts.
_EMBEDDED_IDENTITY_FIELDS: tuple[str, ...] = ("fx_name", "class_id", "module")

#: The revision a `save()`-written entry records. Searching for it costs a pass
#: over the raw text (~1 ms on a 1.4 MB entry) against ~25 ms for the parse, so a
#: superseded entry is dismissed without parsing - the scan stays proportional to
#: the *current* entries rather than to the accumulated history.
_REVISION_MARKER = re.compile(r'"provider_revision"\s*:\s*"([^"]*)"')

#: How many prepared entries one provider may hold in memory. A capability profile
#: is addressed by a key that includes the instance's *live* fingerprint, so the
#: addressable entries of one provider with one material signature are the loaded
#: instances - a handful in any real session. The cap bounds both the memory held
#: for the process's lifetime and (with the newest-first ordering) the scan: once
#: every configured provider is full, the remaining files are not even read, and
#: the ones left out are reported as ``preload.capped`` - never deleted, and still
#: served by the runtime disk path when their key is addressed.
PRELOAD_ENTRY_CAP_PER_PROVIDER = 8


@dataclass
class PreparedEntry:
    """One cache entry that passed every identity-independent check at startup."""

    key: str
    path: str
    directory: str
    provider_id: str
    profile: dict[str, Any]
    validation: ProfileValidation
    content_sha256: str
    counts: dict[str, int]
    size: int
    mtime_ns: int
    #: Whether the entry's own provenance re-derives the ``cache_key`` it records.
    #: True for every provider whose identity carries no host-reported version
    #: (the measured case for the first provider); False is legitimate too - the
    #: key hashes the live ``version``, which the profile does not embed, so a
    #: version-bearing identity cannot be self-derived offline. It is a recorded
    #: fact, not a gate: the admission test is that the **live** key equals ``key``.
    self_derived: bool = False

    def file_unchanged(self) -> bool:
        """Whether the file on disk is still the one this copy was read from.

        A cheap ``stat`` guard, not a re-read: a file whose size or mtime moved
        since startup is not the validated copy any more, and the caller falls
        back to the full disk path rather than serving bytes nothing checked.
        """
        try:
            stat = Path(self.path).stat()
        except OSError:
            return False
        return stat.st_size == self.size and stat.st_mtime_ns == self.mtime_ns

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "provider_id": self.provider_id,
            "controls": self.counts.get("controls"),
            "bytes": self.size,
            "path": self.path,
            "self_derived": self.self_derived,
        }


@dataclass
class PreloadReport:
    """What the startup preload measured, in full."""

    configured_providers: list[str] = field(default_factory=list)
    directory: str | None = None
    files_scanned: int = 0
    bytes_returned: int = 0
    prepared: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    provider_material: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    ran: bool = False
    reason: str | None = None    #: Files not read because every configured provider had already reached
    #: `PRELOAD_ENTRY_CAP_PER_PROVIDER` (newest entries win; nothing is deleted).
    capped: int = 0
    @property
    def n_prepared(self) -> int:
        return len(self.prepared)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped)

    def summary(self) -> dict[str, Any]:
        """The compact form a tool response may carry (never the entry bodies)."""
        return {
            "ran": self.ran,
            "reason": self.reason,
            "directory": self.directory,
            "configured_providers": list(self.configured_providers),
            "files_scanned": self.files_scanned,
            "n_prepared": self.n_prepared,
            "n_skipped": self.n_skipped,
            "capped": self.capped,
            "bytes_returned": self.bytes_returned,
            "seconds": self.seconds,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "prepared": [dict(row) for row in self.prepared],
            "skipped": [dict(row) for row in self.skipped],
            "provider_material": [dict(row) for row in self.provider_material],
        }


class PreparedProfiles:
    """The process-wide index of entries validated at startup (read-only)."""

    def __init__(self) -> None:
        self.entries: dict[str, PreparedEntry] = {}
        self.report: PreloadReport | None = None

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:
        return f"PreparedProfiles(n={len(self.entries)})"

    def put(self, entry: PreparedEntry) -> None:
        self.entries[entry.key] = entry

    def lookup(self, directory: Path | str, key: str) -> PreparedEntry | None:
        """The prepared entry for ``key`` **in this directory**, or ``None``.

        The directory is part of the lookup, not of the key: the index is
        process-wide and a cache directory can be reconfigured, and an entry read
        from another directory is not an answer about this one.
        """
        entry = self.entries.get(key)
        if entry is None:
            return None
        if Path(entry.directory) != Path(directory):
            return None
        return entry

    def clear(self) -> None:
        self.entries.clear()
        self.report = None


_PREPARED: PreparedProfiles | None = None


def prepared_profiles() -> PreparedProfiles:
    """The process-wide prepared index (created on first use)."""
    global _PREPARED
    if _PREPARED is None:
        _PREPARED = PreparedProfiles()
    return _PREPARED


def prepared_lookup(directory: Path | str, key: str) -> PreparedEntry | None:
    return prepared_profiles().lookup(directory, key)


def framing_identity_of(profile: Mapping[str, Any]) -> dict[str, Any] | None:
    """The identity a profile embeds, in the shape the cache key hashes.

    The key hashes the *fingerprint fields*; a profile carries them as
    ``identity.fx_name``/``class_id``/``module`` plus ``identity.fingerprint
    .param_count``. ``version`` is deliberately ``None``: the profile does not
    embed the host-reported version, so a prepared entry can only answer a call
    whose live identity reports none either - which is the case the first
    provider measured (its host reports no plugin version; the plugin's version
    lives in its own document).
    """
    identity = profile.get("identity")
    if not isinstance(identity, Mapping):
        return None
    fingerprint = identity.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        return None
    framing: dict[str, Any] = {name: identity.get(name) for name in _EMBEDDED_IDENTITY_FIELDS}
    framing["param_count"] = fingerprint.get("param_count")
    framing["version"] = None
    return framing


def entry_self_key(entry: Mapping[str, Any]) -> str | None:
    """Re-derive an entry's key from **its own** provenance and fingerprint.

    This is the identity-independent half of the loader's "foreign file" defence
    (AUD-008/AUD-012): an entry whose provenance does not re-derive the key it is
    filed under cannot be shown to be this component's entry. It needs no live
    facts, which is exactly why it can run at startup.
    """
    profile = entry.get("profile")
    provider_id = entry.get("provider_id")
    if not isinstance(profile, Mapping) or not isinstance(provider_id, str) or not provider_id:
        return None
    framing = framing_identity_of(profile)
    if framing is None or framing.get("param_count") is None:
        return None
    provenance = profile.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    return compute_cache_key(
        provider_id=provider_id,
        identity=framing,
        provider_revision=provenance.get("provider_revision"),
        provider_artifact_digest=provenance.get("provider_artifact_digest"),
        source_digest=provenance.get("source_digest"),
        schema_version=entry.get("profile_schema_version"),
        cache_format_key=str(entry.get("cache_format_key")),
    )


def _newest_first(directory: Path) -> list[Path]:
    """Cache entries, newest first, so a capped scan keeps the useful ones.

    The order decides which entries survive ``PRELOAD_ENTRY_CAP_PER_PROVIDER``:
    the entry a session just wrote is the one a next start will address, and the
    old ones stay on disk for the runtime path. ``stat`` failure (a file that
    vanished between ``glob`` and the sort) sorts last instead of raising.
    """
    def stamp(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(directory.glob("profile_*.json"), key=stamp, reverse=True)


def provenance_revision(entry: Mapping[str, Any]) -> str | None:
    """The provider revision an entry's profile declares, when it declares one."""
    profile = entry.get("profile")
    if not isinstance(profile, Mapping):
        return None
    provenance = profile.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    value = provenance.get("provider_revision")
    return None if value is None else str(value)


def superseded_by_text(raw: str, revisions: set[str]) -> bool:
    """Whether the raw text declares a revision that is **not** a live one.

    A prefilter, and only that: it can dismiss a file (no preload - the runtime
    path still reads it when its key is addressed) and it can never admit one
    (the full parse and every check still run afterwards). ``False`` when the
    marker is absent or unreadable - an unrecognized shape takes the slow, full
    path, never a fast guess.
    """
    found = _REVISION_MARKER.findall(raw)
    if not found:
        return False
    return not any(value in revisions for value in found)


def _material_row(provider_id: str, material: Any) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "provider_revision": material.provider_revision,
        "provider_artifact_digest": material.provider_artifact_digest,
        "source_digest": material.source_digest,
        "sources": [row.get("file") for row in material.source_rows],
    }


def preload_from_env(env: Mapping[str, str] | None = None, *, provider_ids: Any = None) -> PreloadReport:
    """Populate the prepared index once; always returns a report, never raises.

    ``provider_ids`` overrides the configured set (tests); otherwise the ids come
    from ``REAPER_MCP_PLUGIN_PROVIDERS`` exactly as the tool layer reads them.
    With no provider configured this is a no-op carrying its reason: a workspace
    that does not use the plugin layer pays nothing.
    """
    report = PreloadReport()
    started = time.perf_counter()
    index = prepared_profiles()
    index.clear()

    from .api import PluginConfig  # noqa: PLC0415 - lazy: avoids an import cycle at module load
    from .providers import registry as provider_registry  # noqa: PLC0415

    config = PluginConfig.from_env(env)
    ids = tuple(str(item).strip().lower() for item in provider_ids) if provider_ids else config.provider_ids
    report.configured_providers = list(ids)
    if not ids:
        report.reason = "no_provider_configured"
        report.seconds = round(time.perf_counter() - started, 4)
        index.report = report
        return report

    try:
        directory = cache_directory(env)
    except PluginError as exc:
        report.reason = exc.code
        report.skipped.append({"file": None, "rule": "cache.directory", "reason": exc.message})
        report.seconds = round(time.perf_counter() - started, 4)
        index.report = report
        return report
    report.directory = str(directory)
    if not directory.is_dir():
        report.reason = "cache_directory_absent"
        report.seconds = round(time.perf_counter() - started, 4)
        index.report = report
        return report

    # key material per configured provider: computed without a build, and a
    # provider that cannot declare it is skipped (its entries cannot be shown to
    # be current) - never guessed.
    materials: dict[str, Any] = {}
    for provider_id in ids:
        try:
            provider = provider_registry.load_provider(provider_id)
            material = key_material_of(provider, {})
        except PluginError as exc:
            report.skipped.append(
                {"file": None, "rule": f"preload.provider:{provider_id}", "reason": f"{exc.code}: {exc.message}"}
            )
            continue
        if material is None:
            report.skipped.append(
                {
                    "file": None,
                    "rule": f"preload.provider:{provider_id}",
                    "reason": "the provider declares no profile_source_rows hook; its entries stay unloaded",
                }
            )
            continue
        materials[provider_id] = material
        report.provider_material.append(_material_row(provider_id, material))
    if not materials:
        report.reason = "no_provider_key_material"
        report.seconds = round(time.perf_counter() - started, 4)
        index.report = report
        return report

    report.ran = True
    per_provider: dict[str, int] = {provider_id: 0 for provider_id in materials}
    revisions_of_provider: dict[str, set[str]] = {
        provider_id: {material.provider_revision} for provider_id, material in materials.items()
    }
    all_revisions: set[str] = {
        material.provider_revision for material in materials.values()
    }
    by_revision: dict[tuple[str, str, str, str], str] = {
        (
            provider_id,
            material.provider_revision,
            material.provider_artifact_digest,
            material.source_digest,
        ): provider_id
        for provider_id, material in materials.items()
    }

    for path in _newest_first(directory):
        if all(
            per_provider.get(provider_id, 0) >= PRELOAD_ENTRY_CAP_PER_PROVIDER
            for provider_id in materials
        ):
            report.capped += 1  # counted, not read: every provider is already full
            continue
        report.files_scanned += 1
        entry, skip = _prepare_file(
            path,
            directory,
            materials,
            by_revision,
            live_revisions=all_revisions,
            revisions_of_provider=revisions_of_provider,
        )
        if entry is None:
            report.skipped.append(skip or {})
            continue
        index.put(entry)
        per_provider[entry.provider_id] = per_provider.get(entry.provider_id, 0) + 1
        report.prepared.append(entry.summary())
        report.bytes_returned += entry.size

    report.seconds = round(time.perf_counter() - started, 4)
    index.report = report
    return report


def _prepare_file(
    path: Path,
    directory: Path,
    materials: dict[str, Any],
    by_revision: dict[tuple[str, str, str, str], str],
    *,
    live_revisions: set[str],
    revisions_of_provider: dict[str, set[str]],
) -> tuple[PreparedEntry | None, dict[str, Any] | None]:
    """Validate one cache file for preload; returns ``(entry, skip_row)``."""
    row: dict[str, Any] = {"file": path.name}
    try:
        stat = path.stat()
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        row.update({"rule": "preload.unreadable", "reason": f"{type(exc).__name__}: {exc}"})
        return None, row
    if superseded_by_text(raw, live_revisions):
        row.update(
            {
                "rule": "preload.superseded",
                "reason": (
                    "the entry declares a provider revision that is not the current one; only "
                    "the runtime path may report or quarantine it (no parse was spent)"
                ),
            }
        )
        return None, row
    try:
        entry = json.loads(raw)
        if not isinstance(entry, dict):
            raise ValueError("not a JSON object")
    except (ValueError, TypeError) as exc:
        row.update({"rule": "preload.unparsable", "reason": str(exc)})
        return None, row

    if entry.get("cache_format_key") != CACHE_FORMAT_KEY:
        row.update({"rule": "preload.format_key", "reason": f"{entry.get('cache_format_key')!r}"})
        return None, row
    if entry.get("profile_schema_version") != PROFILE_SCHEMA_VERSION:
        row.update({"rule": "preload.schema_version", "reason": f"{entry.get('profile_schema_version')!r}"})
        return None, row
    provider_id = entry.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        row.update({"rule": "preload.provider_id", "reason": "the entry names no provider"})
        return None, row
    if provider_id not in materials:
        row.update({"rule": "preload.not_configured", "reason": f"provider {provider_id!r} is not configured"})
        return None, row
    if provenance_revision(entry) not in revisions_of_provider.get(provider_id, set()):
        row.update(
            {
                "rule": "preload.superseded",
                "reason": "the parsed entry declares a revision that is not the current one",
                "entry_revision": provenance_revision(entry),
            }
        )
        return None, row
    profile = entry.get("profile")
    if not isinstance(profile, dict):
        row.update({"rule": "preload.profile_missing", "reason": "no profile object"})
        return None, row

    self_key = entry_self_key(entry)
    if path.name != f"profile_{str(entry.get('cache_key'))[:24]}.json":
        row.update(
            {
                "rule": "preload.filename",
                "reason": "the file name is not the key the entry is filed under",
            }
        )
        return None, row

    material = materials[provider_id]
    provenance = profile.get("provenance") if isinstance(profile.get("provenance"), dict) else {}
    declared = (
        provider_id,
        str(provenance.get("provider_revision")),
        str(provenance.get("provider_artifact_digest")),
        str(provenance.get("source_digest")),
    )
    if declared not in by_revision:
        row.update(
            {
                "rule": "preload.superseded",
                "reason": (
                    "the entry's declared revision/artifact/source digests are not the "
                    "current ones; only the runtime path may report or quarantine it"
                ),
                "entry_revision": provenance.get("provider_revision"),
                "current_revision": material.provider_revision,
            }
        )
        return None, row

    # the identity-independent half of the loader's checks, run once here
    content_sha256 = profile_content_hash(profile)
    if entry.get("content_sha256") != content_sha256:
        row.update({"rule": "preload.content_hash", "reason": "the stored content hash does not match"})
        return None, row
    counts = profile_counts(profile)
    if entry.get("counts") != counts:
        row.update({"rule": "preload.counts", "reason": f"{entry.get('counts')!r} != {counts!r}"})
        return None, row
    validation = validate_profile(profile)
    if validation.rebuild_required or not validation.cacheable:
        row.update(
            {
                "rule": "preload.profile_invalid",
                "reason": "; ".join(validation.codes()) or "not cacheable",
            }
        )
        return None, row

    return (
        PreparedEntry(
            key=str(entry.get("cache_key")),
            path=str(path),
            directory=str(directory),
            provider_id=provider_id,
            profile=profile,
            validation=validation,
            content_sha256=content_sha256,
            counts=counts,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            self_derived=self_key == entry.get("cache_key"),
        ),
        None,
    )
