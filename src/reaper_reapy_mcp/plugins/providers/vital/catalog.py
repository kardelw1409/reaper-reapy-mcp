"""Portable index of the plugin's preset library (Preset Catalog).

Deliberately SEPARATE from the Capability Profile: the preset index has its own
storage, its own invalidation key and its own revision - a new preset file must
never move the profile cache key, and a provider change must never invalidate
preset rows more than the reader revision already does.

**Cache location** follows the repo's existing user-local convention
(`%LOCALAPPDATA%\\reaper_reapy_mcp\\presets` on Windows, `$XDG_CACHE_HOME`
elsewhere), overridable with `REAPER_MCP_VITAL_CATALOG_DIR`. The index is a
cache, never a source of truth: if it is stale, unreadable or absent, the scan
rebuilds from the library itself.

**Key**: the catalog reader revision + a digest over (relative path, size,
mtime_ns) of every `.vital` file under the data root. A changed library rebuilds;
an unchanged one is served from the cache after one stat walk (measured: 2226
files scan in ~0.6 s cold; the stat walk is ~30 ms).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ...errors import InvalidValue

__all__ = [
    "CATALOG_DIR_ENV",
    "CATALOG_SCHEMA_VERSION",
    "CATALOG_REVISION",
    "catalog_directory",
    "Catalog",
]

#: Environment override for the catalog cache directory.
CATALOG_DIR_ENV = "REAPER_MCP_VITAL_CATALOG_DIR"

CATALOG_SCHEMA_VERSION = 1

#: The READER revision: bump when the scan or a row's meaning changes (a row
#: cached by an older reader must not be served as if it matched).
CATALOG_REVISION = "vital-catalog-r1"

_MAX_ROWS_RETURNED = 500


def _user_data_root(env: Mapping[str, str]) -> Path:
    local = env.get("LOCALAPPDATA") or env.get("XDG_CACHE_HOME")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def catalog_directory(env: Mapping[str, str] | None = None) -> Path:
    """The catalog cache directory (override, else user-local)."""
    environ = os.environ if env is None else env
    override = environ.get(CATALOG_DIR_ENV)
    if override is not None:
        if not str(override).strip():
            raise InvalidValue(
                f"{CATALOG_DIR_ENV} is set to an empty value; a literal path is required",
                detail={"env": CATALOG_DIR_ENV},
            )
        return Path(os.path.normpath(os.path.abspath(str(override))))
    root = _user_data_root(environ)
    return Path(os.path.normpath(os.path.abspath(str(root / "reaper_reapy_mcp" / "presets"))))


def library_digest(root: Path) -> tuple[str, list[tuple[str, int, int]]]:
    """(digest, rows) over every `.vital` file's (relpath, size, mtime_ns)."""
    files: list[tuple[str, int, int]] = []
    base = Path(root)
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".vital":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path.relative_to(base).as_posix(), stat.st_size, stat.st_mtime_ns))
    material = json.dumps(files, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(material).hexdigest(), files


class Catalog:
    """The indexed library: scan rows + search, cached by the library digest."""

    def __init__(self, root: Path, *, directory: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self.root = Path(root)
        self.directory = Path(directory) if directory is not None else catalog_directory(env)
        self._rows: list[dict[str, Any]] | None = None
        self._status: dict[str, Any] = {}

    # -- load / build -------------------------------------------------------

    @property
    def cache_file(self) -> Path:
        return self.directory / "vital_catalog.json"

    def rows(self, *, force: bool = False) -> list[dict[str, Any]]:
        if self._rows is not None and not force:
            return self._rows
        digest, files = library_digest(self.root)
        if not force:
            cached = self._read_cache()
            if (
                cached is not None
                and cached.get("schema") == CATALOG_SCHEMA_VERSION
                and cached.get("revision") == CATALOG_REVISION
                and cached.get("library_digest") == digest
                and isinstance(cached.get("rows"), list)
                and len(cached["rows"]) == len(files)
            ):
                self._rows = cached["rows"]
                self._status = {
                    "source": "cache",
                    "built_utc": cached.get("built_utc"),
                    "library_digest": digest,
                    "files": len(files),
                }
                return self._rows
        rows = self._build(files)
        self._rows = rows
        self._status = {"source": "scan", "library_digest": digest, "files": len(files)}
        self._write_cache(digest, rows)
        return rows

    def _build(self, files: list[tuple[str, int, int]]) -> list[dict[str, Any]]:
        from . import preset as preset_module

        rows: list[dict[str, Any]] = []
        for relative, size, mtime_ns in files:
            path = self.root / relative
            try:
                meta = preset_module.read_preset_metadata(path)
            except OSError:
                continue
            rows.append(
                {
                    "id": relative,
                    "name": meta["name"],
                    "embedded_name": meta["embedded_name"],
                    "author": meta["author"],
                    "style": meta["style"],
                    "synth_version": meta["synth_version"],
                    "size": size,
                    "mtime_ns": mtime_ns,
                }
            )
        return rows

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_cache(self, digest: str, rows: list[dict[str, Any]]) -> None:
        payload = {
            "schema": CATALOG_SCHEMA_VERSION,
            "revision": CATALOG_REVISION,
            "library_digest": digest,
            "root": str(self.root),
            "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": rows,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temp = self.cache_file.with_suffix(".json.tmp-%d" % os.getpid())
            text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.cache_file)
        except OSError:
            # A cache that cannot be written is not an error: the scan result is
            # already in memory and the next call simply scans again.
            pass

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    # -- search -------------------------------------------------------------

    def search(
        self,
        *,
        query: str | None = None,
        style: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Token search over name / embedded name / author, plus an exact style filter.

        The tokens mirror the plugin's own browser filter (measured in its
        source: file name + author) and additionally match the embedded
        `preset_name` - 549 of the measured 2226 corpus files carry none, which is
        why the file name is the primary match.
        """
        try:
            limit = int(limit)
            offset = int(offset)
        except (TypeError, ValueError) as exc:
            raise InvalidValue(
                "limit and offset must be integers",
                detail={"limit": limit, "offset": offset},
            ) from exc
        if limit <= 0:
            raise InvalidValue("limit must be positive", detail={"limit": limit})
        if offset < 0:
            raise InvalidValue("offset must not be negative", detail={"offset": offset})
        limit = min(limit, _MAX_ROWS_RETURNED)
        rows = self.rows()
        tokens = [token for token in str(query or "").lower().split() if token]
        wanted_style = str(style or "").strip().lower()
        matched: list[dict[str, Any]] = []
        for row in rows:
            if wanted_style and str(row.get("style") or "").strip().lower() != wanted_style:
                continue
            haystack = " ".join(
                str(row.get(key) or "").lower() for key in ("name", "embedded_name", "author")
            )
            if tokens and not all(token in haystack for token in tokens):
                continue
            matched.append(row)
        matched.sort(key=lambda row: (str(row.get("name") or "").lower(), str(row.get("id") or "")))
        page = matched[offset:offset + limit]
        styles = sorted({str(row.get("style")) for row in rows if row.get("style")})
        return {
            "total": len(matched),
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < len(matched),
            "rows": page,
            "styles": styles,
            "library": self.status(),
        }

    def row(self, preset_id: str) -> dict[str, Any] | None:
        for row in self.rows():
            if row.get("id") == preset_id:
                return row
        return None

    def root_exists(self) -> bool:
        return self.root.is_dir()
