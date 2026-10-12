"""Prepared discovery artifacts: portable, re-checkable, not disposable (P26 §13).

A prepared artifact is the *measured* result of one discovery run, stored beside
the user's other runtime data (``%LOCALAPPDATA%\\reaper_reapy_mcp\\discovery`` by
default, ``REAPER_MCP_PLUGIN_DISCOVERY`` to override) - **not** in the profile
cache, which is disposable by design. Deleting the profile cache must never cost
a deep measurement: this store is what survives it, and a later run can rebuild a
profile from it without touching the host.

Everything that makes a stored artifact safe to *re-use* is checked on read, and
the checks are the P23/P25 lessons applied to a new file format:

- the artifact records the engine version, the identity fingerprint and the
  parameter count it was measured against; a mismatch is a typed refusal, never a
  silent re-use;
- the payload is hashed and the hash is **re-read from disk after the write**
  (``write_text`` translates line endings on Windows, so a digest computed over
  the text that was built is not the digest of the file that exists);
- writes are atomic (temp file + ``os.replace``) and a foreign file at the target
  path is never overwritten;
- an artifact can be **exported and imported**: import re-validates the same way
  a local read does, so a file that travelled between machines is not trusted
  more than a local one.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..model import canonical_json, utc_now_iso
from . import ENGINE_VERSION
from .types import DiscoveryResult

__all__ = [
    "DISCOVERY_DIR_ENV",
    "ARTIFACT_KIND",
    "ARTIFACT_SCHEMA_VERSION",
    "discovery_directory",
    "PreparedArtifact",
    "PreparedStore",
]

#: Environment override for the prepared-artifact directory.
DISCOVERY_DIR_ENV = "REAPER_MCP_PLUGIN_DISCOVERY"

ARTIFACT_KIND = "fx-discovery"
ARTIFACT_SCHEMA_VERSION = 1

#: Identity fields that must match for a prepared artifact to be re-used.
IDENTITY_FIELDS: tuple[str, ...] = ("class_id", "fx_name", "module", "param_count")

_TEMP_INFIX = ".tmp-"
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


def user_data_root(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    local = environ.get("LOCALAPPDATA") or environ.get("XDG_CACHE_HOME")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def discovery_directory(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the prepared-artifact directory (override, else user-local)."""
    environ = os.environ if env is None else env
    override = environ.get(DISCOVERY_DIR_ENV)
    if override is not None:
        if not str(override).strip():
            raise ValueError(f"{DISCOVERY_DIR_ENV} is set to an empty value; a literal path is required")
        return Path(os.path.normpath(os.path.abspath(str(override))))
    return Path(
        os.path.normpath(os.path.abspath(str(user_data_root(environ) / "reaper_reapy_mcp" / "discovery")))
    )


def identity_key(identity: Mapping[str, Any], *, engine_version: str = ENGINE_VERSION) -> str:
    """The artifact's file key: the identity fingerprint plus the engine version.

    The engine version is part of the key, so an algorithm change cannot be
    served an artifact that a previous generation of rules produced.
    """
    material = {field_name: identity.get(field_name) for field_name in IDENTITY_FIELDS}
    material["engine_version"] = engine_version
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:24]


@dataclass
class PreparedArtifact:
    """One stored artifact, as read from disk, with its verification row."""

    document: dict[str, Any] = field(default_factory=dict)
    status: str = "absent"  # ok | absent | refused | corrupt
    rule: str | None = None
    reason: str | None = None
    path: str | None = None
    size: int | None = None
    sha256_matches_disk: bool | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def payload(self) -> dict[str, Any]:
        return dict(self.document.get("payload") or {})

    def observations(self) -> list[dict[str, Any]]:
        return [dict(row) for row in (self.document.get("observations") or [])]

    def meta(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rule": self.rule,
            "reason": self.reason,
            "path": self.path,
            "size": self.size,
            "mode": self.document.get("mode"),
            "engine_version": self.document.get("engine_version"),
            "written_utc": self.document.get("written_utc"),
            "content_sha256": self.document.get("content_sha256"),
            "sha256_matches_disk": self.sha256_matches_disk,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"meta": self.meta(), "payload": self.payload(), "observations": self.observations()}


def _content_digest(payload: Mapping[str, Any], observations: Sequence[Mapping[str, Any]]) -> str:
    material = {
        "payload": payload,
        "observations": list(observations),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


class PreparedStore:
    """Reads and writes prepared discovery artifacts."""

    def __init__(self, *, directory: str | Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(env) if env is not None else None
        self.directory = Path(directory) if directory is not None else discovery_directory(env)
        self.refusals: list[dict[str, Any]] = []

    # -- paths -------------------------------------------------------------

    def path_for(self, identity: Mapping[str, Any], *, engine_version: str = ENGINE_VERSION) -> Path:
        return self.directory / f"discovery_{identity_key(identity, engine_version=engine_version)}.json"

    def paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(self.directory.glob("discovery_*.json"))

    # -- write -------------------------------------------------------------

    def save(
        self,
        result: DiscoveryResult,
        *,
        identity: Mapping[str, Any] | None = None,
        include_observations: bool = True,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Write one artifact atomically and verify the bytes that landed."""
        resolved_identity = dict(identity or result.identity)
        payload = result.to_dict(with_observations=False, with_inventory=True)
        observations = (
            [item.to_dict() for item in result.observations] if include_observations else []
        )
        document: dict[str, Any] = {
            "kind": ARTIFACT_KIND,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "engine_version": result.engine_version,
            "mode": mode or result.mode,
            "written_utc": utc_now_iso(),
            "identity": resolved_identity,
            "compatibility": {
                "engine_version": result.engine_version,
                "param_count": resolved_identity.get("param_count"),
                "channels": [getattr(row, "source", None) for row in (result.inventory.attempts or {}).values()],
                "formats": [row.get("adapter") for row in result.formats],
            },
            "payload": payload,
            "observations": observations,
        }
        document["content_sha256"] = _content_digest(payload, observations)
        text = json.dumps(document, ensure_ascii=False, sort_keys=False, indent=1)
        if len(text) > _MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"the prepared artifact would be {len(text)} bytes, above the {_MAX_ARTIFACT_BYTES} limit"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(resolved_identity, engine_version=result.engine_version)
        temp = target.with_name(target.name + _TEMP_INFIX + secrets.token_hex(4))
        # `newline="\n"` is required: the default translation would write different
        # bytes than the digest describes (P23).
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, target)
        # Verify the bytes that actually landed: re-read the file, parse it and
        # re-derive the content digest. A digest computed over the text that was
        # built is not evidence about the file on disk (and a whole-file digest
        # cannot live *inside* the file it describes, so it is reported here).
        file_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        disk_matches = False
        try:
            raw = target.read_bytes()
            reread = json.loads(raw.decode("utf-8"))
            disk_matches = (
                hashlib.sha256(raw).hexdigest() == file_digest
                and _content_digest(reread.get("payload") or {}, reread.get("observations") or [])
                == document["content_sha256"]
            )
        except (OSError, ValueError):
            disk_matches = False
        row = {
            "path": str(target),
            "size": target.stat().st_size if target.exists() else None,
            "identity_key": identity_key(resolved_identity, engine_version=result.engine_version),
            "content_sha256": document["content_sha256"],
            "file_sha256": file_digest,
            "observations": len(observations),
            "bytes_written": len(text.encode("utf-8")),
            "sha256_matches_disk": bool(disk_matches),
        }
        return row

    # -- read --------------------------------------------------------------

    def load(
        self,
        identity: Mapping[str, Any],
        *,
        engine_version: str = ENGINE_VERSION,
        param_count: int | None = None,
    ) -> PreparedArtifact:
        """Read the artifact for an identity, verifying compatibility first."""
        target = self.path_for(identity, engine_version=engine_version)
        if not target.exists():
            return PreparedArtifact(status="absent", reason="no prepared artifact for this identity", path=str(target))
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return PreparedArtifact(
                status="refused",
                rule="unreadable",
                reason=f"{type(exc).__name__}: {exc}",
                path=str(target),
            )
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            return PreparedArtifact(
                status="corrupt",
                rule="json",
                reason=f"{type(exc).__name__}: {exc}",
                path=str(target),
            )
        artifact = self._validate(document, identity=identity, path=target, engine_version=engine_version)
        artifact.size = len(text.encode("utf-8"))
        artifact.sha256_matches_disk = _recheck_digest(document)
        if artifact.status != "ok":
            self.refusals.append({"path": str(target), "rule": artifact.rule, "reason": artifact.reason})
        return artifact

    def _validate(
        self,
        document: Mapping[str, Any],
        *,
        identity: Mapping[str, Any],
        path: Path,
        engine_version: str,
    ) -> PreparedArtifact:
        artifact = PreparedArtifact(document=dict(document), path=str(path))
        if document.get("kind") != ARTIFACT_KIND:
            artifact.status, artifact.rule = "refused", "kind"
            artifact.reason = f"the file declares kind {document.get('kind')!r}, not {ARTIFACT_KIND!r}"
            return artifact
        if document.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            artifact.status, artifact.rule = "refused", "schema_version"
            artifact.reason = (
                f"artifact schema {document.get('schema_version')!r} is not {ARTIFACT_SCHEMA_VERSION}"
            )
            return artifact
        if document.get("engine_version") != engine_version:
            artifact.status, artifact.rule = "refused", "engine_version"
            artifact.reason = (
                f"the artifact was produced by {document.get('engine_version')!r}, "
                f"this build is {engine_version!r}: the rules that made it have changed"
            )
            return artifact
        stored_identity = document.get("identity") or {}
        mismatches = [
            name
            for name in IDENTITY_FIELDS
            if name != "param_count" and stored_identity.get(name) != identity.get(name)
        ]
        if mismatches:
            artifact.status, artifact.rule = "refused", "identity"
            artifact.reason = "the artifact describes a different instance: " + ", ".join(
                f"{name} {stored_identity.get(name)!r} != {identity.get(name)!r}" for name in mismatches
            )
            return artifact
        live_count = identity.get("param_count")
        stored_count = stored_identity.get("param_count")
        if live_count is not None and stored_count is not None and int(live_count) != int(stored_count):
            artifact.status, artifact.rule = "refused", "param_count"
            artifact.reason = (
                f"the artifact was measured on {stored_count} parameters, the live instance reports {live_count}"
            )
            return artifact
        payload = document.get("payload")
        if not isinstance(payload, Mapping) or not payload.get("controls"):
            artifact.status, artifact.rule = "refused", "payload"
            artifact.reason = "the artifact carries no controls"
            return artifact
        expected = document.get("content_sha256")
        actual = _content_digest(payload, document.get("observations") or [])
        if expected != actual:
            artifact.status, artifact.rule = "corrupt", "content_sha256"
            artifact.reason = f"the recorded content hash {expected!r} does not describe the file ({actual})"
            return artifact
        artifact.status = "ok"
        return artifact

    # -- portability -------------------------------------------------------

    def export(
        self,
        identity: Mapping[str, Any],
        destination: str | Path,
        *,
        engine_version: str = ENGINE_VERSION,
    ) -> dict[str, Any]:
        """Copy a verified artifact to a caller-chosen path."""
        artifact = self.load(identity, engine_version=engine_version)
        if not artifact.ok:
            return {"status": artifact.status, "rule": artifact.rule, "reason": artifact.reason, "path": None}
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = Path(artifact.path or "").read_bytes()
        target.write_bytes(data)
        return {
            "status": "ok",
            "path": str(target),
            "size": len(data),
            "content_sha256": artifact.document.get("content_sha256"),
        }

    def import_file(
        self,
        source: str | Path,
        *,
        identity: Mapping[str, Any] | None = None,
        engine_version: str = ENGINE_VERSION,
    ) -> PreparedArtifact:
        """Validate a foreign artifact exactly as a local one, then store it.

        An imported file is *not* trusted more than a local one: the same
        identity, engine-version and content-hash checks run, and a file that
        fails is returned with its refusal instead of being written.
        """
        origin = Path(source)
        try:
            text = origin.read_text(encoding="utf-8")
            document = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            return PreparedArtifact(
                status="corrupt",
                rule="unreadable",
                reason=f"{type(exc).__name__}: {exc}",
                path=str(origin),
            )
        stored_identity = dict(document.get("identity") or {})
        candidate = dict(identity or stored_identity)
        artifact = self._validate(
            document,
            identity=candidate,
            path=origin,
            engine_version=engine_version,
        )
        if not artifact.ok:
            return artifact
        if identity is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.path_for(candidate, engine_version=engine_version)
            if target.exists() and target != origin:
                existing = self.load(candidate, engine_version=engine_version)
                if existing.ok and existing.document.get("content_sha256") != document.get("content_sha256"):
                    return PreparedArtifact(
                        status="refused",
                        rule="would_overwrite",
                        reason="a different artifact for this identity already exists; it was left in place",
                        path=str(target),
                    )
            target.write_bytes(origin.read_bytes())
            artifact.path = str(target)
        return artifact

    # -- census ------------------------------------------------------------

    def census(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.paths():
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover
                continue
            rows.append({"path": str(path), "size": stat.st_size, "mtime": stat.st_mtime})
        return rows


def _recheck_digest(document: Mapping[str, Any]) -> bool:
    payload = document.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return document.get("content_sha256") == _content_digest(payload, document.get("observations") or [])
