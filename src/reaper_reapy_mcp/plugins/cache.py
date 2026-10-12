"""Capability-profile building and the profile cache (contracts 07, 09).

Two responsibilities live together here because the cache key is derived from
the profile's provenance:

- **build**: wrap a provider-built profile with the provider revision, the
  digest over the provider's declared behaviour-affecting artifacts and the
  digest over the data sources the profile derives from (AUD-008), then run the
  [07 §4] validation table;
- **cache**: one JSON file per key, validate-identity-before-replacement,
  quarantine by atomic rename, atomic temp+replace writes (AUD-012), a
  user-local default directory with a `REAPER_MCP_PLUGIN_CACHE` override
  (AUD-014 Q-07).

Discipline enforced here, not by convention:

- **every** key determinant is load-bearing: a change to the fingerprint, the
  provider revision, the provider artifact digest, the source digest or the
  schema/format changes the key, so a stale profile is never served;
- a **foreign, mismatched or corrupt** file at the expected path is never
  overwritten: it is quarantined by atomic rename and its bytes survive;
- an interrupted write leaves at most an ignorable ``*.tmp-*`` file; the target
  only ever appears through ``os.replace``;
- a profile that fails validation is **used but not cached** (or refused
  entirely when a rebuild is required), and the reason is reported.

Nothing here runs at import time; the directory is resolved when a cache object
is constructed. No host module is imported (08 §8), and cache problems never
raise into a tool call that can still answer - they downgrade to ``built`` with
a warning row (09 §6).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import IncompatibleSettings
from .model import canonical_json, utc_now_iso
from .profile import (
    CACHE_FORMAT_KEY,
    PROFILE_SCHEMA_VERSION,
    ProfileValidation,
    validate_profile,
)

__all__ = [
    "CACHE_DIR_ENV",
    "REQUIRED_FINGERPRINT_FIELDS",
    "FINGERPRINT_FIELDS",
    "ProfileBuild",
    "KeyMaterial",
    "CacheRefusal",
    "CacheStatus",
    "CacheLoad",
    "ProfileCache",
    "user_cache_root",
    "cache_directory",
    "source_tree_marker",
    "source_row",
    "digest_artifact_paths",
    "sources_digest",
    "compute_cache_key",
    "fingerprint_of",
    "profile_content_hash",
    "profile_counts",
    "identity_disagreements",
    "build_capability_profile",
    "key_material_of",
]


#: Environment override for the cache directory (09 §4).
CACHE_DIR_ENV = "REAPER_MCP_PLUGIN_CACHE"

#: Fingerprint fields carried into the cache key (09 §2/§3). `version` presence
#: is tolerated for identity comparison but is always key material (an explicit
#: ``None`` is recorded so an absent version is a value, not a hole).
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "class_id",
    "fx_name",
    "module",
    "param_count",
    "version",
)

#: The subset whose absence means "no usable identity" (fail closed, AUD-008).
REQUIRED_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "class_id",
    "fx_name",
    "module",
    "param_count",
)

#: Volatile provenance stamp fields excluded from the content hash (07 §8:
#: "written_utc-style fields excluded from content hash").
VOLATILE_PROVENANCE_KEYS: tuple[str, ...] = ("generated_utc",)

_TEMP_INFIX = ".tmp-"
_QUARANTINE_INFIX = ".invalid-"

_MISSING_MARKER = "__absent__"


# ---------------------------------------------------------------------------
# Directory policy (09 §4, AUD-014 Q-07)
# ---------------------------------------------------------------------------


def user_cache_root(env: Mapping[str, str] | None = None) -> Path:
    """The platform's user-local data root, never a source tree."""
    environ = os.environ if env is None else env
    local = environ.get("LOCALAPPDATA") or environ.get("XDG_CACHE_HOME")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def cache_directory(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the cache directory: explicit override or user-local default.

    A value **set** for ``REAPER_MCP_PLUGIN_CACHE`` is used verbatim (absolute
    normalisation only; no ``~``/environment expansion, so a typo cannot
    silently resolve somewhere else) and an empty value is a configuration
    error, not "unset" - never a silent fallback.
    """
    environ = os.environ if env is None else env
    override = environ.get(CACHE_DIR_ENV)
    if override is not None:
        if not str(override).strip():
            raise IncompatibleSettings(
                f"{CACHE_DIR_ENV} is set to an empty value; a literal path is required",
                detail={"variable": CACHE_DIR_ENV, "value": str(override), "resolved": None},
            )
        return Path(os.path.normpath(os.path.abspath(str(override))))
    return Path(os.path.normpath(os.path.abspath(str(user_cache_root(environ) / "reaper_reapy_mcp" / "cache" / "plugins"))))


def source_tree_marker(path: str | Path) -> Path | None:
    """The source-control marker found at/above ``path``, or ``None``.

    Used to assert that the default cache directory is not inside a source tree
    and to warn when a deliberately configured one is (09 §4: the cache is
    runtime data and must be git-ignored if placed in-tree).
    """
    current = Path(path)
    if current.suffix:  # a file: start from its directory
        current = current.parent
    try:
        current = current.resolve()
    except OSError:  # pragma: no cover - a dangling path still walks literally
        current = Path(os.path.abspath(str(current)))
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.exists():
            return marker
    return None


# ---------------------------------------------------------------------------
# Key material
# ---------------------------------------------------------------------------


def _present(value: Any) -> str:
    """An explicit marker for an absent determinant: a missing component must
    never hash like an empty one (a digest over nothing is not evidence)."""
    if value is None:
        return _MISSING_MARKER
    text = str(value)
    return text if text else _MISSING_MARKER


def source_row(path: str | Path) -> dict[str, Any]:
    """A provenance row for one data file actually read by the provider.

    A file that cannot be read yields ``sha256: None``; the [07 §4] validation
    then refuses the cache write, so an unreadable source can never produce a
    cacheable profile.
    """
    candidate = Path(path)
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        return {"file": str(candidate), "sha256": None, "unreadable": f"{type(exc).__name__}: {exc}"}
    return {"file": str(candidate), "sha256": hashlib.sha256(data).hexdigest()}


def digest_artifact_paths(paths: Sequence[str | Path]) -> tuple[str, list[dict[str, Any]]]:
    """Digest over the provider's behaviour-affecting artifacts (AUD-008).

    Returns ``(digest, rows)``. An empty path list still yields a deterministic,
    explicit digest (never a bare hash of nothing) and the validation table's
    provenance row is what decides cacheability.
    """
    rows = [source_row(path) for path in sorted(str(path) for path in paths)]
    payload = canonical_json({"kind": "provider-artifacts", "rows": rows})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), rows


def sources_digest(sources: Sequence[Mapping[str, Any]] | None) -> str:
    """Digest over the provider's declared data sources (09 §3)."""
    rows: list[dict[str, Any]] = []
    for source in sources or []:
        if not isinstance(source, Mapping):
            continue
        rows.append({"file": str(source.get("file")), "sha256": source.get("sha256")})
    rows.sort(key=lambda row: (row["file"], str(row["sha256"])))
    if not rows:
        return hashlib.sha256(b"provider-sources:none").hexdigest()
    payload = canonical_json({"kind": "provider-sources", "rows": rows})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_of(identity: Mapping[str, Any]) -> dict[str, Any]:
    """The fingerprint fields of an identity, every field explicit."""
    return {name: identity.get(name) for name in FINGERPRINT_FIELDS}


def compute_cache_key(
    *,
    provider_id: str,
    identity: Mapping[str, Any],
    provider_revision: Any,
    provider_artifact_digest: Any,
    source_digest: Any,
    schema_version: int = PROFILE_SCHEMA_VERSION,
    cache_format_key: str = CACHE_FORMAT_KEY,
) -> str:
    """The normative key of 09 §3 - all five determinants, none optional."""
    material = {
        "provider_id": _present(provider_id),
        "fingerprint": fingerprint_of(identity),
        "provider_revision": _present(provider_revision),
        "provider_artifact_digest": _present(provider_artifact_digest),
        "source_digest": _present(source_digest),
        "profile_schema_version": schema_version,
        "cache_format_key": _present(cache_format_key),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def profile_content_hash(profile: Mapping[str, Any]) -> str:
    """Content hash over the profile with the volatile stamps excluded."""
    material = json.loads(json.dumps(profile, default=str))
    provenance = material.get("provenance")
    if isinstance(provenance, dict):
        for key in VOLATILE_PROVENANCE_KEYS:
            provenance.pop(key, None)
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def profile_counts(profile: Mapping[str, Any]) -> dict[str, int]:
    """The count cross-check column: a lost tail must fail a second,
    independent check besides the content hash (09 §4)."""
    controls = [row for row in (profile.get("controls") or []) if isinstance(row, Mapping)]
    modules = [row for row in (profile.get("modules") or []) if isinstance(row, Mapping)]
    measured_laws = 0
    for control in controls:
        law = control.get("law")
        if isinstance(law, Mapping) and law.get("kind") not in (None, "none", "unmeasured"):
            measured_laws += 1
    return {"controls": len(controls), "modules": len(modules), "measured_laws": measured_laws}


def identity_disagreements(profile: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    """How a cached profile's identity disagrees with the live identity.

    Required fields must agree exactly; ``param_count`` is compared against the
    embedded fingerprint; ``version`` follows 07 §3.5 - absence is tolerated,
    a *present* pair that differs is a disagreement.
    """
    embedded = profile.get("identity") if isinstance(profile.get("identity"), Mapping) else {}
    disagreements: dict[str, Any] = {}
    for name in ("fx_name", "class_id", "module"):
        if identity.get(name) is not None and embedded.get(name) != identity.get(name):
            disagreements[name] = {"profile": embedded.get(name), "live": identity.get(name)}
    fingerprint = embedded.get("fingerprint") if isinstance(embedded.get("fingerprint"), Mapping) else {}
    live_count = identity.get("param_count")
    if live_count is not None and fingerprint.get("param_count") != live_count:
        disagreements["param_count"] = {"profile": fingerprint.get("param_count"), "live": live_count}
    live_version = identity.get("version")
    profile_version = embedded.get("version")
    if live_version is not None and profile_version is not None and live_version != profile_version:
        disagreements["version"] = {"profile": profile_version, "live": live_version}
    return disagreements


# ---------------------------------------------------------------------------
# Profile building (07 §1/§4; AUD-008)
# ---------------------------------------------------------------------------


@dataclass
class KeyMaterial:
    """The cache-key determinants a provider can declare **without a build**.

    This is what makes a warm cache hit cheap: the key is computable from the
    provider's declared revision, the digest over its behaviour-affecting source
    files and the digest over the data sources it would read - no profile is
    projected, no registry is parsed. The build path remains the authority for
    *content*, and `resolve_instance_profile` cross-checks that the built
    profile's own provenance re-derives this material's key before anything is
    filed under it (`cache.key_material` refusal otherwise).
    """

    provider_revision: str
    provider_artifact_digest: str
    source_digest: str
    source_rows: list[dict[str, Any]] = field(default_factory=list)
    artifact_rows: list[dict[str, Any]] = field(default_factory=list)
    declared_by: str = "provider.profile_source_rows"

    def key_for(self, provider_id: str, identity: Mapping[str, Any]) -> str:
        return compute_cache_key(
            provider_id=provider_id,
            identity=identity,
            provider_revision=self.provider_revision,
            provider_artifact_digest=self.provider_artifact_digest,
            source_digest=self.source_digest,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_revision": self.provider_revision,
            "provider_artifact_digest": self.provider_artifact_digest,
            "source_digest": self.source_digest,
            "source_rows": [dict(row) for row in self.source_rows],
            "artifact_rows": [dict(row) for row in self.artifact_rows],
            "declared_by": self.declared_by,
        }


def key_material_of(provider: Any, identity: Mapping[str, Any]) -> "KeyMaterial | None":
    """The key determinants without building the profile, when the provider can
    declare its source rows (optional hook, 09 §3).

    ``None`` means "this provider does not offer the hook": the caller then uses
    the established build-then-load order, which is correct but pays a full
    build even on a cache hit. A hook that raises a typed `PluginError` (an
    unusable registry, for instance) propagates: the failure is the provider's
    own refusal and must not be turned into a silent fallback.
    """
    hook = getattr(provider, "profile_source_rows", None)
    if hook is None or not callable(hook):
        return None
    rows = hook(dict(identity))
    if rows is None:
        return None
    source_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    artifact_digest, artifact_rows = digest_artifact_paths(list(provider.artifact_paths()))
    return KeyMaterial(
        provider_revision=str(getattr(provider, "implementation_revision", "") or ""),
        provider_artifact_digest=artifact_digest,
        source_digest=sources_digest(source_rows),
        source_rows=source_rows,
        artifact_rows=artifact_rows,
    )


def adopt_profile(provider: Any, profile: Mapping[str, Any]) -> bool:
    """Hand the profile that will be served to the provider (optional hook).

    The provider's rule evaluator is built against the profile it last produced
    (`dependencies`), so a cache hit that skipped the build must
    still adopt the served profile - otherwise a provider-side consumer that
    reads its own state before `normalize_request` runs would silently evaluate
    against an empty profile. Returns True when a hook was called.
    """
    hook = getattr(provider, "adopt_profile", None)
    if hook is None or not callable(hook):
        return False
    hook(dict(profile))
    return True


@dataclass
class ProfileBuild:
    """A provider-built profile wrapped with Core-computed provenance."""

    profile: dict[str, Any]
    validation: ProfileValidation
    cacheable: bool
    provider_revision: str
    provider_artifact_digest: str
    source_digest: str
    artifact_rows: list[dict[str, Any]] = field(default_factory=list)
    source_rows: list[dict[str, Any]] = field(default_factory=list)
    #: True when the profile was served from the cache instead of built now.
    from_cache: bool = False

    def key_for(self, provider_id: str, identity: Mapping[str, Any]) -> str:
        return compute_cache_key(
            provider_id=provider_id,
            identity=identity,
            provider_revision=self.provider_revision,
            provider_artifact_digest=self.provider_artifact_digest,
            source_digest=self.source_digest,
        )

    @classmethod
    def from_cached_profile(
        cls,
        *,
        profile: dict[str, Any],
        validation: ProfileValidation,
        material: "KeyMaterial",
    ) -> "ProfileBuild":
        """A build record for a profile the cache served, using live material.

        The validation is the one the load path already ran over the entry, so a
        cache hit does not re-validate the same bytes a second time.
        """
        return cls(
            profile=profile,
            validation=validation,
            cacheable=(not validation.rebuild_required) and validation.cacheable,
            provider_revision=material.provider_revision,
            provider_artifact_digest=material.provider_artifact_digest,
            source_digest=material.source_digest,
            artifact_rows=[dict(row) for row in material.artifact_rows],
            source_rows=[dict(row) for row in material.source_rows],
            from_cache=True,
        )


def build_capability_profile(
    provider: Any, identity: Mapping[str, Any], *, now: str | None = None
) -> ProfileBuild:
    """Build a profile through the provider and complete its provenance.

    The provider owns profile *content* (its data); the Core owns the
    determinants of the *cache key*: the declared implementation revision, the
    digest over the declared artifacts and the digest over the declared
    sources. A missing revision or digest therefore refuses only the cache
    write - never a silent placeholder (AUD-008).
    """
    profile = provider.build_profile(dict(identity))
    if not isinstance(profile, dict):
        raise IncompatibleSettings(
            "the provider's build_profile did not return an object",
            detail={"provider": str(getattr(provider, "id", "?")), "type": type(profile).__name__},
        )
    provenance = dict(profile.get("provenance") or {})
    provenance["generated_utc"] = now or utc_now_iso()
    provenance.setdefault("generator", f"{getattr(provider, 'id', 'provider')}/provider")

    revision = str(getattr(provider, "implementation_revision", "") or "")
    provenance["provider_revision"] = revision

    artifact_digest, artifact_rows = digest_artifact_paths(list(provider.artifact_paths()))
    provenance["provider_artifact_digest"] = artifact_digest
    provenance["provider_artifacts"] = artifact_rows

    digest = sources_digest(provenance.get("sources"))
    provenance["source_digest"] = digest
    profile["provenance"] = provenance

    validation = validate_profile(profile)
    cacheable = (not validation.rebuild_required) and validation.cacheable
    return ProfileBuild(
        profile=profile,
        validation=validation,
        cacheable=cacheable,
        provider_revision=revision,
        provider_artifact_digest=artifact_digest,
        source_digest=digest,
        artifact_rows=artifact_rows,
        source_rows=[
            {"file": str(row.get("file")), "sha256": row.get("sha256")}
            for row in (provenance.get("sources") or [])
            if isinstance(row, Mapping)
        ],
    )


# ---------------------------------------------------------------------------
# Cache status objects
# ---------------------------------------------------------------------------


@dataclass
class CacheRefusal:
    """One refusal with its reason and the quarantine destination if any."""

    rule: str
    reason: str
    quarantine_path: str | None = None

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {"rule": f"cache.{self.rule}", "reason": self.reason}
        if self.quarantine_path is not None:
            row["quarantine"] = self.quarantine_path
        return row


@dataclass
class CacheStatus:
    """What `plugin_describe.meta.cache` reports (09 §4)."""

    key: str = ""
    source: str = "none"  # cache | built | rebuilt | none
    path: str | None = None
    refusals: list[CacheRefusal] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    #: True when the entry was answered from the process's startup preload index
    #: (a validated in-memory copy) rather than read and re-validated from disk.
    prepared: bool = False
    #: True when the profile was built this call (the warm path must stay False;
    #: this is the counter an "is the prepared profile reused" test asserts on).
    built: bool = False
    #: True when the cache was consulted without projecting a profile first.
    key_without_build: bool = False

    def snapshot(self) -> "CacheStatus":
        """A copy of the status **at this moment**.

        A resolution must keep reporting what it actually did: handing out the
        live object would let a later call rewrite an earlier answer, so a
        reader would be told the second call's key and source for the first
        call's result (the same failure class as a mutable counter inside a
        recorded artifact).
        """
        return CacheStatus(
            key=self.key,
            source=self.source,
            path=self.path,
            refusals=list(self.refusals),
            quarantined=list(self.quarantined),
            warnings=[dict(row) for row in self.warnings],
            prepared=self.prepared,
            built=self.built,
            key_without_build=self.key_without_build,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "path": self.path,
            "prepared": self.prepared,
            "built": self.built,
            "key_without_build": self.key_without_build,
            "refusals": [refusal.as_row() for refusal in self.refusals],
            "quarantined": list(self.quarantined),
            "warnings": [dict(row) for row in self.warnings],
        }


@dataclass
class CacheLoad:
    """Outcome of one load attempt: ``hit`` uses the file, ``miss`` builds,
    ``refused`` quarantined whatever sat at the path and builds."""

    status: str
    path: str
    profile: dict[str, Any] | None = None
    validation: ProfileValidation | None = None
    refusal: CacheRefusal | None = None


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


class ProfileCache:
    """Key-addressed, validate-before-replace, crash-safe profile cache."""

    def __init__(
        self,
        *,
        provider_id: str,
        directory: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        now: Any = None,
    ) -> None:
        self.provider_id = str(provider_id)
        self.directory = (
            Path(directory) if directory is not None else cache_directory(env)
        )
        self._now = now or utc_now_iso
        self._persistent_warnings: list[dict[str, Any]] = []
        self._status = CacheStatus()
        marker = source_tree_marker(self.directory)
        if marker is not None:
            self._persistent_warnings.append(
                {
                    "rule": "cache",
                    "reason": (
                        "the cache directory is inside a source-control tree "
                        f"({marker}); it holds runtime data and must be git-ignored (09 §4)"
                    ),
                    "path": str(self.directory),
                }
            )
        self._status.warnings.extend(dict(row) for row in self._persistent_warnings)

    # -- paths / status ----------------------------------------------------

    @property
    def status(self) -> CacheStatus:
        return self._status

    def begin_call(self) -> CacheStatus:
        """Start a fresh status record for one resolution.

        The status must never be cumulative: a refusal from an earlier call
        would otherwise be reported inside a later call's ``meta.cache`` (and
        would wrongly downgrade a plain ``built`` to ``rebuilt``).
        """
        self._status = CacheStatus(warnings=[dict(row) for row in self._persistent_warnings])
        return self._status

    def path_for(self, key: str) -> Path:
        """Content-addressable file name: one JSON file per key (09 §4).

        The prefix is 24 hex characters (96 bits), not 12: the directory is
        shared by every provider, and a short prefix lets one key's file sit
        where another key's file is expected - which could quarantine a valid
        entry. A collision at 96 bits is not a reachable event.
        """
        return self.directory / f"profile_{key[:24]}.json"

    def _stamp(self) -> str:
        """A filesystem-safe UTC stamp: the ISO form contains ``:`` (illegal in
        Windows file names), so the separators are dropped, not the instant."""
        return str(self._now()).replace("-", "").replace(":", "").replace(".", "")

    def _record(self, refusal: CacheRefusal) -> None:
        self._status.refusals.append(refusal)
        if refusal.quarantine_path:
            self._status.quarantined.append(refusal.quarantine_path)

    def refuse_no_identity(self, missing: Sequence[str], *, reason: str) -> None:
        """The fail-closed identity path: no cache read and no cache write."""
        self._status.source = "none"
        self._record(CacheRefusal("identity", f"{reason}: {list(missing)}"))

    def refuse_key_material(self, *, built_key: str, material_key: str, provider_id: str) -> None:
        """The fast-path consistency refusal (never silently ignored).

        A provider that declares its key material without a build must satisfy
        one property: the profile it *would* build derives the same key. When it
        does not, the provider's declaration and its provenance disagree - the
        write is refused rather than filing a profile under a key its own
        provenance does not re-derive (which the loader would quarantine later,
        after the fact).
        """
        self._status.source = "built"
        self._record(
            CacheRefusal(
                "key_material",
                "the provider's declared key material does not re-derive the key its built "
                f"profile carries (provider {provider_id!r}: declared {material_key[:24]}…, "
                f"built {built_key[:24]}…); the profile is served but not cached",
            )
        )
        self._status.warnings.append(
            {
                "rule": "cache.key_material",
                "reason": "declared_material_disagrees_with_built_provenance",
                "provider": provider_id,
                "material_key": material_key,
                "built_key": built_key,
            }
        )

    # -- load --------------------------------------------------------------

    def load(self, key: str, *, identity: Mapping[str, Any] | None = None) -> CacheLoad:
        """Read the entry for ``key``; validate it completely before use.

        A prepared entry from the process's startup preload index answers first
        (its identity-independent checks already ran at startup); anything the
        prepared copy cannot prove - a file that moved since startup, an identity
        disagreement - falls through to the full disk path, which reports and
        quarantines exactly as before.
        """
        target = self.path_for(key)
        self._status.key = key
        self._status.path = str(target)
        prepared = self._prepared_entry(key, identity=identity)
        if prepared is not None:
            self._status.source = "cache"
            self._status.prepared = True
            return prepared
        if not target.exists():
            return CacheLoad(status="miss", path=str(target))

        outcome = self._read_entry(target, key, identity)
        if outcome.status == "hit":
            self._status.source = "cache"
            return outcome
        self._status.source = "rebuilt" if self._status.refusals else self._status.source
        return outcome

    def _prepared_entry(self, key: str, *, identity: Mapping[str, Any] | None) -> CacheLoad | None:
        """The in-memory answer, when the preload index still describes the file.

        Two things are re-checked here because they depend on facts the preload
        could not know: the file itself must be the one that was prepared (size
        and mtime), and - when a live identity is supplied - the cached profile's
        identity must agree with the instance in front of us. The re-derivation
        of the key from the entry's own provenance also ran at preload time (it
        needs no live facts) and is what makes a prepared entry addressable here.
        """
        from . import preload as _preload  # noqa: PLC0415 - lazy: keeps imports acyclic

        entry = _preload.prepared_lookup(self.directory, key)
        if entry is None:
            return None
        if not entry.file_unchanged():
            return None
        if identity is not None and identity_disagreements(entry.profile, identity):
            return None
        return CacheLoad(
            status="hit",
            path=entry.path,
            profile=entry.profile,
            validation=entry.validation,
        )

    def _read_entry(
        self, target: Path, key: str, identity: Mapping[str, Any] | None
    ) -> CacheLoad:
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            return self._quarantine(
                target, "unreadable", f"the cache file cannot be read: {type(exc).__name__}: {exc}"
            )
        try:
            entry = json.loads(raw)
            if not isinstance(entry, dict):
                raise ValueError("the cache file is not a JSON object")
        except (ValueError, TypeError) as exc:
            return self._quarantine(target, "unparsable", f"the cache file does not parse: {exc}")

        if entry.get("cache_format_key") != CACHE_FORMAT_KEY:
            return self._quarantine(
                target,
                "format_key",
                f"cache_format_key {entry.get('cache_format_key')!r} is not {CACHE_FORMAT_KEY!r}",
            )
        if entry.get("cache_key") != key:
            return self._quarantine(
                target,
                "foreign_key",
                "the file at the computed key's path carries a different key; "
                "it is not this component's entry (AUD-012)",
            )
        if entry.get("profile_schema_version") != PROFILE_SCHEMA_VERSION:
            return self._quarantine(
                target,
                "schema_version",
                f"profile_schema_version {entry.get('profile_schema_version')!r} is not "
                f"{PROFILE_SCHEMA_VERSION!r}",
            )
        profile = entry.get("profile")
        if not isinstance(profile, dict):
            return self._quarantine(target, "profile_missing", "the entry carries no profile object")

        validation = validate_profile(profile)
        if validation.rebuild_required or not validation.cacheable:
            return self._quarantine(
                target,
                "profile_invalid" if validation.rebuild_required else "not_cacheable",
                f"the cached profile may not be used: {validation.codes()} "
                "(09 §6: a validated-profile cache entry is never written or served)",
            )

        stored_hash = entry.get("content_sha256")
        actual_hash = profile_content_hash(profile)
        if not isinstance(stored_hash, str) or stored_hash != actual_hash:
            return self._quarantine(
                target,
                "content_hash",
                "the stored content hash does not match the recomputed hash "
                "(truncated or tampered entry)",
            )

        stored_counts = entry.get("counts")
        actual_counts = profile_counts(profile)
        if not isinstance(stored_counts, dict) or stored_counts != actual_counts:
            return self._quarantine(
                target,
                "counts",
                f"the stored counts {stored_counts!r} disagree with the recomputed {actual_counts!r}",
            )

        # Which provider's entry this is does **not** depend on the live identity, so
        # the check cannot live inside the identity guard below. The cache directory is
        # shared by every provider and the key is provider-scoped, so a foreign key can
        # only be addressed by a mistake - and serving it would hand one plugin's
        # capability profile to another plugin (P24's cross-provider test found this
        # reachable: a cache built for provider B served provider A's profile as a *hit*
        # when addressed with A's key). Refused, **not** quarantined: the entry is valid -
        # for its own provider - and destroying a working entry because somebody asked
        # for the wrong key would be a worse bug than the one fixed.
        entry_provider = entry.get("provider_id")
        if isinstance(entry_provider, str) and entry_provider and entry_provider != self.provider_id:
            refusal = CacheRefusal(
                "provider_id",
                f"the entry belongs to provider {entry_provider!r} and this cache files "
                f"{self.provider_id!r}; a profile is served only through its own provider",
            )
            self._status.source = "none"
            self._record(refusal)
            return CacheLoad(status="refused", path=str(target), refusal=refusal)

        if identity is not None:
            disagreements = identity_disagreements(profile, identity)
            if disagreements:
                return self._quarantine(
                    target,
                    "identity",
                    "the cached profile's identity disagrees with the live instance: "
                    f"{sorted(disagreements)}",
                )
            provider_id = entry.get("provider_id")
            provenance = profile.get("provenance") if isinstance(profile.get("provenance"), dict) else {}
            if not (isinstance(provider_id, str) and provider_id):
                return self._quarantine(
                    target,
                    "key_material",
                    "the entry does not name the provider whose key it is filed under; "
                    "it cannot be shown to be this component's entry (AUD-008)",
                )
            if True:
                recomputed = compute_cache_key(
                    provider_id=provider_id,
                    identity=identity,
                    provider_revision=provenance.get("provider_revision"),
                    provider_artifact_digest=provenance.get("provider_artifact_digest"),
                    source_digest=provenance.get("source_digest"),
                    schema_version=entry.get("profile_schema_version"),
                    cache_format_key=str(entry.get("cache_format_key")),
                )
                if recomputed != key:
                    return self._quarantine(
                        target,
                        "key_material",
                        "the entry's own provenance does not re-derive the key it is filed "
                        "under; the file is not this component's validated entry (AUD-008)",
                    )

        return CacheLoad(status="hit", path=str(target), profile=profile, validation=validation)

    def _quarantine(self, target: Path, rule: str, reason: str) -> CacheLoad:
        """Quarantine by **atomic rename** - the bytes are preserved, the path
        is reported, and the caller rebuilds (09 §5.3-.5). A rename that itself
        fails is reported loudly and nothing is deleted.

        The quarantine path is made collision-free before the rename: a second
        quarantine within the same second must never land on (and therefore
        overwrite) the first one's bytes - a preserved file that a later run
        silently replaces is not preserved.
        """
        quarantine = self._free_quarantine_path(target)
        recorded = str(quarantine)
        try:
            os.rename(target, quarantine)
        except OSError as exc:
            recorded = ""
            reason = f"{reason}; quarantine rename failed: {type(exc).__name__}: {exc}"
            self._status.warnings.append({"rule": "cache", "reason": reason, "path": str(target)})
        refusal = CacheRefusal(rule, reason, recorded or None)
        self._record(refusal)
        return CacheLoad(status="refused", path=str(target), refusal=refusal)

    def _free_quarantine_path(self, target: Path) -> Path:
        base = f"{target.name}{_QUARANTINE_INFIX}{self._stamp()}"
        candidate = target.with_name(base)
        counter = 0
        while candidate.exists():
            counter += 1
            candidate = target.with_name(f"{base}-{counter}")
        return candidate

    # -- save --------------------------------------------------------------

    def save(self, key: str, profile: dict[str, Any]) -> CacheStatus:
        """Write the entry for ``key`` via temp file + ``os.replace``.

        Never overwrites a file it did not author: an existing file at the
        target path is fully validated first (that is also the crash-recovery
        path), and an entry that cannot be shown to be this component's own is
        quarantined before the fresh write.
        """
        self._status.key = key
        target = self.path_for(key)
        self._status.path = str(target)

        profile_provider = str(profile.get("provider") or "")
        if profile_provider != self.provider_id:
            # A provider that hands this cache another provider's profile has a bug;
            # filing it would make that bug permanent and addressable. Refused, and
            # the bytes are not touched (there is nothing at the path to touch yet).
            self._record(
                CacheRefusal(
                    "provider_id",
                    f"the profile is the {profile_provider!r} provider's and this cache files "
                    f"{self.provider_id!r} entries",
                )
            )
            return self._status

        validation = validate_profile(profile)
        if validation.rebuild_required or not validation.cacheable:
            self._record(
                CacheRefusal(
                    "not_cacheable",
                    f"the profile may not be cached: {validation.codes()}",
                )
            )
            return self._status

        if target.exists():
            existing = self._read_entry(target, key, identity=None)
            if existing.status == "hit":
                if profile_content_hash(existing.profile or {}) == profile_content_hash(profile):
                    self._status.source = "cache" if not self._status.refusals else self._status.source
                    return self._status  # idempotent: the entry is already ours
                self._quarantine(
                    target,
                    "content_drift",
                    "a file at this key's path exists with different content; "
                    "the bytes are preserved and a fresh entry is written",
                )
            elif existing.refusal is not None and existing.refusal.quarantine_path is None:
                # the occupied bytes could not be moved aside: publishing now would
                # destroy them, so nothing is written and the state is reported
                self._record(
                    CacheRefusal(
                        "occupied",
                        "a file that is not this component's entry sits at the target path and "
                        "the quarantine rename failed; refusing to publish over it (AUD-012)",
                    )
                )
                return self._status

        entry = {
            "cache_format_key": CACHE_FORMAT_KEY,
            "provider_id": self.provider_id,
            "cache_key": key,
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "written_utc": self._now(),
            "counts": profile_counts(profile),
            "content_sha256": profile_content_hash(profile),
            "profile": profile,
        }
        try:
            blob = json.dumps(entry, indent=2, sort_keys=True)
        except (TypeError, ValueError) as exc:
            self._record(
                CacheRefusal(
                    "not_serializable",
                    f"the profile cannot be serialized into a cache entry: {type(exc).__name__}: {exc}",
                )
            )
            return self._status
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._record(
                CacheRefusal("directory", f"the cache directory cannot be created: {exc}")
            )
            return self._status

        tmp = target.with_name(f"{target.name}{_TEMP_INFIX}{os.getpid()}-{secrets.token_hex(4)}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            self._discard_temp(tmp)
            self._record(
                CacheRefusal(
                    "write",
                    "the cache write did not complete; the previous file (if any) is "
                    f"untouched and at most a temporary file remains: {exc}",
                )
            )
            return self._status

        if self._status.source not in ("rebuilt",):
            self._status.source = "rebuilt" if self._status.refusals else "built"
        return self._status
    @staticmethod
    def _discard_temp(tmp: Path) -> None:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_entry_file(path: str | Path) -> dict[str, Any] | None:
    """Test/support helper: read a cache file as written (no validation)."""
    try:
        entry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return entry if isinstance(entry, dict) else None


def temp_files_in(directory: str | Path) -> list[Path]:
    """Every leftover temp file in a cache directory (B-56 inspection)."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if _TEMP_INFIX in path.name)


def entry_files_in(directory: str | Path) -> list[Path]:
    """Every entry file (never a temp file, never a quarantined one) in a
    cache directory."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    found: list[Path] = []
    for path in folder.iterdir():
        name = path.name
        if not name.startswith("profile_") or not name.endswith(".json"):
            continue
        if _TEMP_INFIX in name or _QUARANTINE_INFIX in name:
            continue
        found.append(path)
    return sorted(found)


def quarantined_files_in(directory: str | Path) -> list[Path]:
    """Every quarantined file in a cache directory, oldest name first."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if _QUARANTINE_INFIX in path.name)
