"""The Vital `PluginProvider` (08 §2, §7).

Vital is the first provider, not a template: everything specific to it - its
identity strings, its registry, its document format, its depth fits and its rule
table - lives in this package, and Core sees only the contracts.

Two honest boundaries are stated here rather than discovered later:

1. **The measured version.** The host reports no plugin version; Vital's version
   lives in its state document (`synth_version`). `identify` therefore carries
   whatever the host gives it, the profile declares the measured set, and
   `version_gate()` is the pure check. Wiring the *document* version into a
   write's preflight needs either a provider hook Core calls with the decoded
   document or a Core change; until then a write cannot be gated on a live
   document version, and this is recorded as an open item (not claimed).
2. **The parameter ident.** Core's parameter channel is ident-addressed, and the
   ident is host-provided (there is no offline source for it). A control write
   whose binding has no ident is therefore **refused with a named reason** rather
   than silently writing nothing - a silent no-op would be reported as a
   successful write.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from ...binding import declared_names as _declared_names
from ...binding import normalise_name as _normalise
from ...errors import (
    InvalidRoute,
    InvalidValue,
    NoConversion,
    PluginError,
    UnsupportedControl,
    VersionMismatch,
)
from ...model import (
    CaptureScope,
    Finding,
    FxHostFacts,
    ResolvedRequest,
    RouteSpec,
    TimingProfile,
    VerificationPolicy,
)
from ...provider import DependencyRuleSet
from . import calibration as calibration_module
from . import enum_boundaries as enum_boundaries_module
from . import codec as codec_module
from . import dependencies_rules as rules_module
from . import depth as depth_module
from . import wavetables as wavetables_module
from .codec import VitalCodec
from .profile_build import (
    DEFAULT_REGISTRY_DIR,
    REGISTRY_DIR_ENV,
    SUPPORTED_VERSIONS,
    VITAL_CLASS_ID,
    VITAL_FX_NAME,
    VITAL_MODULE,
    VitalRegistry,
    build_profile,
    load_registry,
    registry_source_rows,
)

__all__ = ["IMPLEMENTATION_REVISION", "VitalProvider"]

#: Bumped on ANY semantic change to a declared artifact (07 §3.4): the cache key
#: covers provider logic, so a behaviour change without a bump is a release bug.
#: r2 (P8): live ident binding verification + the `document_version` hook.
#: r3 (P8): the codec's document locator (first brace) and its pad insertion
#: point (before the closing brace) - a live run found both.
#: r4 (P8): plugin-derived leaves are declared, refused as write targets and
#: excluded from a restore with a named gap.
#: r5 (2026-10-11): route creation is refused before the first write because
#: `route_into_off_filter` cannot be evaluated (the guard is a behaviour change:
#: a route create that used to be applied is now a typed refusal).
#: r6 (2026-10-11): `route_into_off_filter` is **evaluated** - the destination
#: ->module join is derived from the registry (316/316 destinations) and the
#: module's on/off flag comes from the live document the projected world now
#: carries - so route creation is available and only a decided violation (a pair
#: into an OFF Filter 1/Filter 2) refuses. Undecidable subjects still refuse with
#: the missing input named.
#: r7 (2026-10-11): the provider declares its cache-key material without a build
#: (`profile_source_rows` hashes the registry instead of parsing it) and adopts
#: the profile the cache served (`adopt_profile`) - a warm call no longer projects
#: the profile it will discard. Both are additive to the observable profile
#: content; the revision moves because the key must move with the semantics
#: (07 §3.4) unless the release argues it should not.
#: r8 (2026-10-11): the **host-calibration artifact** (a preparation-run file whose
#: bytes are a declared source, so its hash is key material): descending laws
#: derived from a measured sweep, units filled from the host's own formatted text,
#: per-state law confirmations, host step/toggle metadata, and `law_candidate`
#: records for sweeps a law nearly fits. The profile schema moves to 2 with it.
#: r9 (2026-10-11): the **measured enum band boundaries** - a second preparation
#: artifact (also a declared source) holding, per enum control, the boundary
#: between each adjacent pair of printed states located through the read-only host
#: formatter on arbitrary normalized values. `switch_points`/`positions` now come
#: from that measurement for every enum whose bands were measurable, with the
#: evidence level attached; controls whose display turned out to be a ramp
#: (no band structure) and controls whose members the host never prints are named
#: instead of being given an invented map.
#: r10 (2026-10-12): the measured bands are carried with the profile
#: (`boundary_evidence.band_ends`), the merge refuses a control whose write target
#: could fall outside its own band or whose band *interiors* the measurement did not
#: confirm, a `confirmed_within` bracket must contain the profile's boundary, and the
#: profiler's `positions`/`switch_points` values are validated against the bands.
#: r11 (2026-10-12): a declared `enum_index` whose *measured* display changes at every
#: swept position and follows one numeric law is served as a **number** with that law
#: (20 controls), the law is fitted at the sweep's own stored positions with the
#: float32 storage term in the budget (the four `random_N_frequency` ratios), and the
#: enum-boundaries merge records `enum_declaration_overridden` / withholds a
#: confirmation the artifact did not measure / refuses a numeric model the artifact
#: contradicts. The profile for those controls therefore *changes meaning* at an
#: unchanged identity, and the key must move with the semantics (07 §3.4) - as r7 put
#: it. (The key would also move on its own: `provider_artifact_digest` hashes this
#: provider's own source files, so an edit to the builder is key material too. The
#: revision is what *declares* the semantics generation, and it is the determinant a
#: reader can see without diffing the code.)
#: r12 (2026-10-12): **range integrity** - the plugin's own parameter table
#: (`source_facts.py`, verified against the live host formatter on 7,650 samples)
#: replaces a declared range wherever a 900-preset corpus range truncated the
#: parameter (70 controls; widen only, never narrow or shift, with the before/after
#: and the display transform in `evidence.value_bounds` and the counts in the
#: profile's `value_bounds` block). Two integer ladders (`effect_chain_order`,
#: `sample_transpose_quantize`) gain their proven engine step, which the core applies
#: in both conversion directions, and the percent-unit trap no longer fires on a
#: **fraction** value space whose `%` is only the display's unit. Ranges are part of
#: what a stored profile means, so the key must move (07 §3.4).
#: r13 (2026-10-13): the **wavetable channel** (`wavetables.py` + the codec's guard).
#: A caller can now name a table by source (`wavetable` request -> `settings.wavetables.<i>`),
#: and every write into a slot - semantic or raw `state` path - is guarded against the
#: measured host-abort boundary (schema, no growth beyond what the engine holds, and
#: `version` stamped from the live document, because the plugin restamps it). The write
#: channel and the file it lives in are part of what the provider declares, so the key
#: must move (07 §3.4).
#: r14 (2026-10-17): the **preset library** (`preset.py`, `catalog.py`) and the codec's
#: measured write laws (P38): a state write is planned as growth-budgeted batches (a
#: unit that grows the document past the measured band silently does not land), the eight
#: top-level labels are writable, and the sample block is carried/verified up to the
#: plugin's measured 4-sample reload delay. The provider now owns a capability
#: (`presets()`) and its label/sample write surface - both are behaviour the profile and
#: the tool layer depend on, so the key must move (07 §3.4).
IMPLEMENTATION_REVISION = "vital-provider-2026.10-r14"

_PROVIDER_FILES = (
    "provider.py",
    "codec.py",
    "profile_build.py",
    "dependencies_rules.py",
    "depth.py",
    "calibration.py",
    "enum_boundaries.py",
    "source_facts.py",
    "wavetables.py",
    "preset.py",
    "catalog.py",
    "__init__.py",
)

_STATE_ROOT = codec_module.MUTABLE_ROOT

#: The fields a `wavetable` request spec may carry. Closed on purpose: a typo
#: (`fromPreset`, `slot`) must refuse rather than write a table nobody chose.
_WAVETABLE_SPEC_KEYS = {"oscillator", "from_preset", "preset_slot", "table_id"}


class VitalProvider:
    """Vital Audio's Vital, VST3 build, version set measured in the registry."""

    id = "vital"
    implementation_revision = IMPLEMENTATION_REVISION

    def __init__(
        self,
        *,
        registry: VitalRegistry | None = None,
        registry_dir: str | None = None,
        calibration_path: str | None = None,
        enum_boundaries_path: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._registry_dir = registry_dir
        self._calibration_path = calibration_path
        self._enum_boundaries_path = enum_boundaries_path
        self._env = env
        self._last_profile: dict[str, Any] | None = None
        self._timing = TimingProfile(settle_s=0.4, retry_after_s=2.0, retry_count=1, timeout_s=20.0)

    # -- configuration (read on first use, never at import) -----------------

    def registry(self) -> VitalRegistry:
        """The loaded registry; loads and validates on first use."""
        if self._registry is None:
            self._registry = load_registry(directory=self._registry_dir, env=self._env)
        return self._registry

    def artifact_paths(self) -> list[str]:
        """Behaviour-affecting files of this provider (hashed into the cache key)."""
        from pathlib import Path

        here = Path(__file__).parent
        return [str(here / name) for name in _PROVIDER_FILES]

    def profile_source_rows(self, identity: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """The registry provenance rows for the cache key, **without a build**.

        This is the hook that makes a warm cache hit cheap: the key's
        `source_digest` needs the digest of every data file the profile derives
        from, and hashing five files costs a fraction of the ~6.5 MB parse the
        profile projection performs. The rows are the same ones `build_profile`
        declares - if the loaded registry is already in hand its own rows are
        returned verbatim, and otherwise the files are resolved and hashed by the
        same rules (`profile_build.registry_source_rows`).

        The host-calibration artifact, when one exists at the resolved path, is a
        declared source too: its bytes decide which units/confirmations the
        profile carries, so a changed artifact must move the key (and a created or
        removed one must invalidate the entry).

        A registry that is unusable raises the same typed `IncompatibleSettings`
        the build would raise: the fast path never invents key material.

        **Freshness equals the instance's lifetime**: a provider that has already
        loaded its registry reports the hashes taken at load. The tool layer
        constructs a provider per call (`api._providers`), so in production the
        hashes are re-taken every call - memoizing provider instances would
        freeze `source_digest` and must come with its own invalidation.
        """
        if self._registry is not None:
            rows = [
                {"file": row.get("file"), "sha256": row.get("sha256")}
                for row in self._registry.sources
                if isinstance(row, Mapping)
            ]
        else:
            rows = registry_source_rows(directory=self._registry_dir, env=self._env)
        calibration_row = self._calibration_source_row()
        if calibration_row is not None:
            rows.append(calibration_row)
        enum_row = self._enum_boundaries_source_row()
        if enum_row is not None:
            rows.append(enum_row)
        return rows

    def calibration_artifact_path(self) -> str | None:
        """The resolved calibration path (explicit > env > user-local default).

        Resolution never expands ``~``/env characters and an empty override is a
        configuration error (the registry's own rules, 08 §6.1).
        """
        return calibration_module.resolve_calibration_path(self._calibration_path, self._env)

    def calibration_configured(self) -> bool:
        """Whether a calibration was *asked for* (explicit argument or override).

        The difference matters for honesty: a default location that holds no file
        is a normal state (no preparation run yet) and is reported as *absent*; a
        path that was configured and cannot be read is a **refusal** the caller
        must see.
        """
        if self._calibration_path is not None:
            return True
        environ = os.environ if self._env is None else self._env
        return environ.get(calibration_module.CALIBRATION_ENV) is not None

    def _calibration_source_row(self) -> dict[str, Any] | None:
        """The calibration file's hash, or ``None`` when no usable file exists.

        Read-only and parse-free (the fast path hashes, it does not decode). An
        *unreadable* file yields no row: a source row without a hash would make
        every profile non-cacheable, and an unreadable artifact contributes
        nothing to the profile anyway - it is reported instead (see
        `build_profile`).
        """
        path = self.calibration_artifact_path()
        if not path:
            return None
        candidate = Path(path)
        try:
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return None
        # hashed in chunks: the key must include the artifact's bytes even when a
        # malformed artifact is huge, and the fast path must not read it into memory
        # to do so (the loader, which does decode, has its own size bound)
        return {"file": candidate.name, "sha256": digest.hexdigest()}

    def enum_boundaries_artifact_path(self) -> str | None:
        """The resolved measured-boundaries path (explicit > env > user-local default)."""
        return enum_boundaries_module.resolve_enum_boundaries_path(
            self._enum_boundaries_path, self._env
        )

    def enum_boundaries_configured(self) -> bool:
        """Whether a measured-boundaries artifact was *asked for* (see `calibration_configured`)."""
        if self._enum_boundaries_path is not None:
            return True
        environ = os.environ if self._env is None else self._env
        return environ.get(enum_boundaries_module.ENUM_BOUNDARIES_ENV) is not None

    def _enum_boundaries_source_row(self) -> dict[str, Any] | None:
        """The boundaries artifact's hash, or ``None`` when no usable file exists."""
        path = self.enum_boundaries_artifact_path()
        if not path:
            return None
        candidate = Path(path)
        try:
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return None
        return {"file": candidate.name, "sha256": digest.hexdigest()}

    def adopt_profile(self, profile: Mapping[str, Any]) -> None:
        """Adopt the profile Core will serve, so provider-side consumers agree.

        The dependency-rule evaluator and the structural DISABLE set are built
        against "the last profile this provider produced" (`dependencies`,
        `disable_targets`). On a cache hit nothing is produced, so Core hands the
        served profile over explicitly - otherwise a consumer reached before
        `normalize_request` would silently evaluate against an empty profile.
        """
        if isinstance(profile, Mapping) and profile:
            self._last_profile = dict(profile)

    # -- identity -----------------------------------------------------------

    def matches(self, facts: FxHostFacts) -> bool:
        """Vital's VST3 build only; a VST2 build is not this provider's target.

        The measured discriminators are the class id (32 hex) and the module
        string; the name alone is not evidence, and a build whose module is
        `vital.vst2` or whose class differs is refused by **not matching**
        (fail closed: no profile, no describe, no write).
        """
        if "vital" not in (facts.fx_name or "").lower():
            return False
        if (facts.class_id or "").upper() != VITAL_CLASS_ID.upper():
            return False
        return (facts.module or "").lower() == VITAL_MODULE.lower()

    def identify(self, facts: FxHostFacts) -> dict[str, Any]:
        """The profile fingerprint as the host reports it right now (pure)."""
        identity: dict[str, Any] = {
            "fx_name": facts.fx_name,
            "class_id": facts.class_id,
            "module": facts.module,
            "param_count": facts.param_count,
        }
        if facts.version is not None:
            identity["version"] = facts.version
        return identity

    def version_gate(self, profile: Mapping[str, Any], live_version: str | None) -> None:
        """07 §3.2: a live version outside the measured set refuses writes."""
        if live_version is None:
            return
        compatible = [str(item) for item in (profile.get("identity") or {}).get("compatible_versions") or []]
        if str(live_version) not in compatible:
            raise VersionMismatch(
                f"the live Vital version {live_version!r} is not in the measured set {sorted(compatible)}; "
                "writes are refused and no override exists in V1",
                detail={"live_version": live_version, "compatible_versions": sorted(compatible), "override": None},
            )

    def document_version(self, document: Any) -> str | None:
        """The **live** version carried by a decoded document (07 §3.2).

        Measured live (P8, 2026-10-10): Vital's document carries
        ``synth_version`` (``"1.5.5"`` on the loaded instance), and the host
        reports no plugin version of its own - so this is the only live version
        a write can be gated on. Core calls this hook if it exists; a provider
        without a document-side version simply does not implement it.
        """
        if not isinstance(document, Mapping):
            return None
        value = document.get("synth_version")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # -- profile ------------------------------------------------------------

    def build_profile(self, identity: dict[str, Any]) -> dict[str, Any]:
        """The registry projection plus whatever the calibration artifact justifies.

        The artifact is loaded, identity-checked and applied here; a file that
        exists but cannot be used (unparsable, wrong method version, measured on
        another build) is **not** applied and the profile carries the reason while
        its hash still travels in `provenance.sources` - a changed artifact must
        invalidate the cache entry even when it is refused.
        """
        row = self._calibration_source_row()
        calibration = None
        note: str | None = None
        path = self.calibration_artifact_path()
        if path:
            if row is None:
                if self.calibration_configured():
                    note = "the configured calibration artifact is not readable; nothing was applied"
            else:
                try:
                    loaded = calibration_module.load_calibration(path)
                except PluginError as exc:
                    note = f"{exc.code}: {exc.message}"
                else:
                    if loaded.matches_identity(identity):
                        calibration = loaded
                    else:
                        note = (
                            "the artifact was measured on a different build than this "
                            "identity; nothing was applied"
                        )

        enum_row = self._enum_boundaries_source_row()
        enum_boundaries = None
        enum_note: str | None = None
        boundaries_path = self.enum_boundaries_artifact_path()
        if boundaries_path:
            if enum_row is None:
                if self.enum_boundaries_configured():
                    enum_note = (
                        "the configured enum-boundary artifact is not readable; nothing was applied"
                    )
            else:
                try:
                    loaded_boundaries = enum_boundaries_module.load_enum_boundaries(boundaries_path)
                except PluginError as exc:
                    enum_note = f"{exc.code}: {exc.message}"
                else:
                    if loaded_boundaries.matches_identity(identity):
                        enum_boundaries = loaded_boundaries
                    else:
                        enum_note = (
                            "the artifact was measured on a different build than this "
                            "identity; nothing was applied"
                        )
        profile = build_profile(
            self.registry(),
            identity,
            calibration=calibration,
            calibration_row=row,
            calibration_note=note,
            enum_boundaries=enum_boundaries,
            enum_boundaries_row=enum_row,
            enum_boundaries_note=enum_note,
        )
        self._last_profile = profile
        return profile

    # -- request normalization ---------------------------------------------

    def normalize_request(self, profile: dict[str, Any], request: dict[str, Any]) -> ResolvedRequest:
        """Resolve names/values against the profile; no host access.

        Refusals are typed problem rows, never silent skips:
        unknown control; a law of `none`/`unmeasured` (no conversion is possible
        and none is invented); a value outside the declared range or hitting the
        ``%`` trap; an unbound parameter ident; a route outside the measured
        vocabulary (the POC's reason: an unknown destination fails *silently* in
        the plugin).
        """
        resolved = ResolvedRequest()
        self._last_profile = profile or self._last_profile
        controls = list(profile.get("controls") or [])

        for name, value in dict(request.get("controls") or {}).items():
            control = self._control_by_name(controls, name)
            if control is None:
                resolved.problems.append(
                    {
                        "control": name,
                        "code": "UnsupportedControl",
                        "message": f"unknown control {name!r}",
                        "detail": {"suggestions": self._suggest(name, controls)},
                    }
                )
                continue
            # The **shared** value resolution (P29): one vocabulary for both
            # paths, so `{"normalized": x}` means the same thing whichever
            # profile answered - and a display request still needs its confirmed
            # conversion, which is why this path's laws are used here too.
            from ... import request_value as request_value_module
            from ... import values as values_module

            outcome = request_value_module.resolve_control_value(
                control, value, values_module=values_module
            )
            if "problem" in outcome:
                resolved.problems.append(outcome["problem"])
                continue
            binding = dict(control.get("binding") or {})
            if not binding.get("ident"):
                unverified = binding.get("unverified") or {}
                reason = str(unverified.get("reason") or "ident_not_bound")
                resolved.problems.append(
                    {
                        "control": name,
                        "code": "UnsupportedControl",
                        "message": (
                            "the parameter ident of this control is not bound to a verified live "
                            "address; an unbound write is refused instead of silently writing "
                            "nothing (the binding is measured against the host on every write)"
                        ),
                        "detail": {
                            "control": control.get("id"),
                            "index": binding.get("index"),
                            "reason": reason,
                            "observed_name": unverified.get("observed_name"),
                            "declared_names": unverified.get("declared_names"),
                        },
                    }
                )
                continue
            row = dict(outcome["row"])
            row["channel"] = str((control.get("authority") or {}).get("write") or "parameter")
            resolved.controls[control["id"]] = row

        vocabulary_sources = set(str(key) for key in (profile.get("modulation") or {}).get("sources") or [])
        vocabulary_destinations = set(
            str(key) for key in (profile.get("modulation") or {}).get("destinations") or []
        )
        for spec in request.get("routes_create") or []:
            source = spec.get("source")
            destination = spec.get("destination")
            if source not in vocabulary_sources or destination not in vocabulary_destinations:
                resolved.problems.append(
                    {
                        "route": dict(spec),
                        "code": "InvalidRoute",
                        "message": (
                            f"route vocabulary violation: {source!r} -> {destination!r}; an unknown "
                            "destination fails silently in the plugin, so it is refused here"
                        ),
                        "detail": {
                            "reason": "vocabulary",
                            "sources": sorted(vocabulary_sources)[:40],
                            "destinations_count": len(vocabulary_destinations),
                        },
                    }
                )
                continue
            amount = spec.get("amount")
            if amount is None and spec.get("depth") is not None:
                try:
                    amount = self.convert_depth(profile, str(destination), spec["depth"])
                except (NoConversion, InvalidValue) as exc:
                    resolved.problems.append(
                        {"route": dict(spec), "code": exc.code, "message": exc.message, "detail": exc.detail}
                    )
                    continue
            if amount is None:
                resolved.problems.append(
                    {
                        "route": dict(spec),
                        "code": "InvalidValue",
                        "message": "a route needs an amount or a depth to convert",
                        "detail": {"reason": "missing_amount"},
                    }
                )
                continue
            entry = {
                "source": str(source),
                "destination": str(destination),
                "amount": float(amount),
                "bipolar": float(spec.get("bipolar") or 0.0),
            }
            # P38: a create may name the slot it lands in (a preset apply reproduces
            # the file's own slot numbering, because the per-slot `modulation_N_*`
            # amounts live at that index). The codec validates that the slot is free;
            # a non-integer here is a typed refusal rather than a silent drop.
            if spec.get("slot") is not None:
                try:
                    entry["slot"] = int(spec["slot"])
                except (TypeError, ValueError):
                    resolved.problems.append(
                        {
                            "route": dict(spec),
                            "code": "InvalidRoute",
                            "message": "a create's `slot` must be an integer route slot number",
                            "detail": {"reason": "bad_slot", "slot": spec.get("slot")},
                        }
                    )
                    continue
            resolved.routes_create.append(entry)

        for key, target in (("routes_update", resolved.routes_update), ("routes_remove", resolved.routes_remove)):
            for spec in request.get(key) or []:
                if spec.get("slot") is None:
                    resolved.problems.append(
                        {
                            "route": dict(spec),
                            "code": "InvalidRoute",
                            "message": f"a {key.split('_')[1]} must name the route's slot identity",
                            "detail": {"reason": "missing_slot"},
                        }
                    )
                    continue
                entry = {k: v for k, v in spec.items() if v is not None}
                if key == "routes_update" and entry.get("depth") is not None:
                    # Measured defect D1 (2026-10-11, P20): only `routes_create` converted a
                    # semantic depth, so the documented update example
                    # (`{"slot": 3, "depth": -6}`, 06 §8/§9) reached the codec with a field it
                    # does not know and was refused there as "an update names no field to
                    # change" - a misleading error for a well-formed request. The conversion
                    # itself is the same one create uses; it needs the **destination**, which
                    # for an existing route lives in the live table (not readable here, this
                    # method does no host access), so a caller that does not name it gets a
                    # refusal that says exactly that instead of a later, confusing one.
                    if entry.get("amount") is not None:
                        # an explicit native amount wins; `depth` is never carried into the write
                        entry.pop("depth")
                    elif not entry.get("destination"):
                        resolved.problems.append(
                            {
                                "route": dict(spec),
                                "code": "InvalidValue",
                                "message": (
                                    "a semantic `depth` on an existing route needs the destination to convert "
                                    "it: pass `destination` (from `plugin_modulation(op='list')`) or pass the "
                                    "native `amount`"
                                ),
                                "detail": {"reason": "depth_without_destination", "slot": entry.get("slot")},
                            }
                        )
                        continue
                    else:
                        try:
                            entry["amount"] = self.convert_depth(profile, str(entry["destination"]), entry.pop("depth"))
                        except (NoConversion, InvalidValue) as exc:
                            resolved.problems.append(
                                {"route": dict(spec), "code": exc.code, "message": exc.message, "detail": exc.detail}
                            )
                            continue
                target.append(entry)

        for path, value in dict(request.get("state") or {}).items():
            key = str(path)
            # Top-level LABEL paths (preset metadata) are writable since P38
            # (measured live: all eight land and read back inside the write
            # budget); everything else must be rooted at the document's settings.
            if not key.startswith(f"{_STATE_ROOT}.") and key not in codec_module.LABEL_PATHS:
                resolved.problems.append(
                    {
                        "path": path,
                        "code": "InvalidValue",
                        "message": f"state path {path!r} is not rooted at {_STATE_ROOT!r}",
                        "detail": {"roots": [_STATE_ROOT], "labels": list(codec_module.LABEL_PATHS)},
                    }
                )
                continue
            resolved.state[key] = value

        # Wavetable selection (P30): the table is document state and has NO host
        # parameter (measured: the 775-control inventory holds only `*_wave_frame`,
        # the position INSIDE a table). A caller therefore names a table by its
        # SOURCE - a `.vital` preset on disk plus a slot - and this converts it into
        # the same `settings.wavetables.<i>` path the raw state channel uses. The
        # write itself stays one channel, and the guard that bounds it (schema, no
        # growth, version stamping) lives in the codec, which is the only place that
        # sees the LIVE document.
        wavetable_specs = request.get("wavetable")
        if wavetable_specs is not None and not isinstance(wavetable_specs, list):
            wavetable_specs = [wavetable_specs]
        for spec in wavetable_specs or []:
            if not isinstance(spec, dict):
                resolved.problems.append(
                    {
                        "wavetable": spec,
                        "code": "InvalidValue",
                        "message": "a wavetable spec must be an object",
                        "detail": {"accepted_keys": sorted(_WAVETABLE_SPEC_KEYS)},
                    }
                )
                continue
            unknown = sorted(set(str(key) for key in spec) - _WAVETABLE_SPEC_KEYS)
            if unknown:
                resolved.problems.append(
                    {
                        "wavetable": dict(spec),
                        "code": "InvalidValue",
                        "message": f"unknown wavetable spec field(s): {', '.join(unknown)}",
                        "detail": {
                            "accepted_keys": sorted(_WAVETABLE_SPEC_KEYS),
                            "example": {
                                "oscillator": 1,
                                "from_preset": "C:/path/to/some.vital",
                                "preset_slot": 1,
                                "table_id": "optional content check",
                            },
                        },
                    }
                )
                continue
            try:
                slot = wavetables_module.check_slot(spec.get("oscillator"))
            except InvalidValue as exc:
                resolved.problems.append(
                    {"wavetable": dict(spec), "code": exc.code, "message": exc.message, "detail": exc.detail}
                )
                continue
            preset = spec.get("from_preset")
            if not isinstance(preset, str) or not preset:
                resolved.problems.append(
                    {
                        "wavetable": dict(spec),
                        "code": "InvalidValue",
                        "message": (
                            "a wavetable spec needs `from_preset`: the table's content is what "
                            "identifies it, and a preset file is the only source of one this "
                            "layer can read (the plugin exposes no host-visible table list)"
                        ),
                        "detail": {"reason": "missing_from_preset", "accepted_keys": sorted(_WAVETABLE_SPEC_KEYS)},
                    }
                )
                continue
            slot_path = wavetables_module.path_for(slot)
            if slot_path in resolved.state:
                resolved.problems.append(
                    {
                        "wavetable": dict(spec),
                        "code": "InvalidValue",
                        "message": (
                            f"two changes address the same wavetable slot ({slot_path!r}): the raw "
                            "`state` request and this spec would race for it"
                        ),
                        "detail": {"reason": "duplicate_slot", "path": slot_path},
                    }
                )
                continue
            if any(row.get("path") == slot_path for row in resolved.wavetable):
                resolved.problems.append(
                    {
                        "wavetable": dict(spec),
                        "code": "InvalidValue",
                        "message": f"two wavetable specs address the same slot ({slot_path!r})",
                        "detail": {"reason": "duplicate_slot", "path": slot_path},
                    }
                )
                continue
            try:
                found = wavetables_module.read_slot_from_preset(
                    preset,
                    oscillator=slot,
                    preset_slot=spec.get("preset_slot"),
                    expected_table_id=spec.get("table_id"),
                )
            except PluginError as exc:
                resolved.problems.append(
                    {"wavetable": dict(spec), "code": exc.code, "message": exc.message, "detail": exc.detail}
                )
                continue
            resolved.state[slot_path] = found["value"]
            resolved.wavetable.append(
                {
                    "oscillator": slot,
                    "path": slot_path,
                    "source": found["source"],
                    "preset": found["preset"],
                    "preset_slot": found["preset_slot"],
                    "table_id": found["descriptor"]["table_id"],
                    "n_frames": found["descriptor"]["n_frames"],
                    "payload_bytes": found["descriptor"]["payload_bytes"],
                }
            )

        resolved.structural = bool(request.get("structural"))
        if resolved.structural:
            # The DISABLE set is a provider-side write on the caller's behalf, so
            # every target must be a path the profile **measured** and a value in
            # the document's own space. Spelling the candidate name as a path
            # created phantom keys instead (measured 2026-10-10: 13 keys created,
            # no real setting changed, and the plan verified itself against the
            # keys it had just invented); an unmappable or unjustifiable target is
            # now a typed refusal (Decision 1).
            planned: list[str] = []
            for row in resolved.controls.values():
                control = row.get("control")
                if isinstance(control, dict):
                    planned.extend(_declared_names(control))
            to_disable = {_normalise(name) for name in rules_module.disable_set(planned)}
            for target in rules_module.disable_targets(profile):
                if _normalise(target["candidate"]) not in to_disable:
                    continue  # the plan keeps this one on
                reason = target.get("reason")
                if reason:
                    resolved.problems.append(
                        {
                            "state_path": target.get("path") or target["candidate"],
                            "code": "PlanError",
                            "message": (
                                f"the structural DISABLE target {target['candidate']!r} cannot be "
                                "addressed in the live document"
                            ),
                            "detail": {
                                "reason": reason,
                                "candidate": target["candidate"],
                                "off_value": target.get("off"),
                                "explanation": target.get("detail"),
                                "why_disable": target.get("why"),
                            },
                        }
                    )
                    continue
                resolved.state.setdefault(str(target["path"]), target["value"])
        return resolved

    def _control_by_name(self, controls: list[dict[str, Any]], name: Any) -> dict[str, Any] | None:
        wanted = str(name).strip().lower()
        for control in controls:
            candidates = {str(control.get("id", "")).lower(), str(control.get("label", "")).lower()}
            candidates.update(str(alias).lower() for alias in control.get("aliases") or [])
            if wanted in candidates:
                return control
        return None

    def _suggest(self, name: Any, controls: list[dict[str, Any]], limit: int = 3) -> list[str]:
        wanted = str(name).lower()
        scored: list[tuple[int, str]] = []
        for control in controls:
            control_id = str(control.get("id", ""))
            score = sum(1 for token in wanted.replace(" ", "_").split("_") if token and token in control_id.lower())
            if score:
                scored.append((score, control_id))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [control_id for _score, control_id in scored[:limit]]

    # -- depth --------------------------------------------------------------

    def convert_depth(self, profile: dict[str, Any], destination: str, depth: Any) -> float:
        """Semantic depth -> native amount, or `NoConversion` with the reason."""
        registry = self.registry()
        result = depth_module.modulation_depth(registry.rows(), registry.scales(), str(destination), float(depth))
        return float(result["amount"])

    def depth_table(self, *, destinations: list[str] | None = None) -> dict[str, Any]:
        registry = self.registry()
        return depth_module.depth_table(registry.rows(), registry.scales(), destinations)

    # -- rules / policy -----------------------------------------------------

    def dependencies(self) -> DependencyRuleSet:
        """The measured rule table plus the projected-world evaluator.

        The evaluator is built against the last profile this provider produced,
        because a rule needs the profile's display mapping; Core builds the
        profile before it runs a transaction (P4 wiring).
        """
        return rules_module.dependencies(self._last_profile or {})

    def unevaluated_rules(self) -> list[dict[str, Any]]:
        """Honest status of every declared rule this provider does not decide.

        Core attaches this list to a mutation's response (dry-run included), so a
        caller is told what was **not** checked instead of reading an empty
        findings list as "everything was checked" (Decision 1).
        """
        return rules_module.rules_not_evaluated()

    def disable_targets(self) -> list[dict[str, Any]]:
        """The structural DISABLE set as addressable targets, or named refusals."""
        return rules_module.disable_targets(self._last_profile or {})

    def codec(self) -> VitalCodec:
        return VitalCodec()

    def state_view(self, document: Any) -> dict[str, Any]:
        """The read view `plugin_state` attaches (optional hook, P30).

        It answers the one question the parameter inventory cannot: **which table each
        oscillator reads**. The table is document state (measured: no host parameter
        selects it; the only wave-related controls are `*_wave_frame`, the position
        INSIDE a table), so a caller needs the content ids and frame counts to know what
        a write changed. Descriptors only - never the wave data.
        """
        return {"wavetables": wavetables_module.describe_slots(document or {})}

    def presets(self) -> Any:
        """The plugin's NATIVE preset library (P38) - the capability `plugin_presets` uses.

        Returns an object exposing `search/info/plan/save` (see
        `preset.PresetCapability`). The tool layer treats a provider without this
        hook as one without a preset capability and refuses typed - never silently.
        """
        from . import preset as preset_module

        return preset_module.PresetCapability()

    def guarded_state_paths(self, profile: dict[str, Any]) -> list[str]:
        """State paths a DECLARED-BUT-NOT-EVALUATED hard rule refuses to enter (P38).

        The transaction refuses any plan that writes one of these subjects (the
        `fm_needs_modulator` guard), so a bulk change set - a preset apply - must
        know them BEFORE planning and name the skip as a gap rather than let the
        whole apply die on one control. Derived from the rules table and the
        profile's own declared names (never re-typed here), so a new guard moves
        this list with it.
        """
        mapping = rules_module._subject_paths(profile or {})
        return sorted(set(mapping.values()))

    def write_order(self) -> tuple[str, ...]:
        """Fixed document -> parameters (the measured ordering rule)."""
        return ("state", "parameters")

    def verification_policy(self) -> VerificationPolicy:
        return VerificationPolicy(
            tolerances={"state.float": 1e-5, "parameter.normalized": 1e-3},
            comparators={"enum": "exact", "number": "abs_le"},
            timing=self._timing,
        )

    def capture_scope(self) -> CaptureScope:
        return CaptureScope(
            channels=["document", "routes", "parameters"],
            parameters={"scope": "full_vector", "ident_required": True},
            restore_order=["document", "parameters"],
            verify_order=["document", "parameters"],
        )

    def limits(self) -> list[str]:
        profile = self._last_profile
        if profile and profile.get("limits"):
            return [str(item) for item in profile["limits"]]
        return [
            "VST2 build not supported",
            f"only the measured version(s) {sorted(SUPPORTED_VERSIONS)} are accepted",
            "one route per state write",
            "no line_mapping writes",
            "a wavetable write replaces the table the engine holds and may not GROW it "
            "(measured host-abort boundary: a larger payload does not land and once aborted "
            "the host); the table is selected by content, and `*_wave_frame` only moves "
            "INSIDE the selected table",
            "a control write is issued only when the host's parameter name at the declared "
            "index matches the control (verified live on every write; a mismatch refuses)",
        ]

    # -- diagnostics --------------------------------------------------------

    def settings_report(self) -> dict[str, Any]:
        """Where the registry was resolved from and what it declares."""
        registry = self.registry()
        return {
            "registry_dir": registry.directory,
            "variable": REGISTRY_DIR_ENV,
            "default_dir": DEFAULT_REGISTRY_DIR,
            "files": dict(registry.files),
            "measured_version": registry.version,
            "compatible_versions": sorted({registry.version or SUPPORTED_VERSIONS[0], *SUPPORTED_VERSIONS}),
            "fake_name": VITAL_FX_NAME,
        }
