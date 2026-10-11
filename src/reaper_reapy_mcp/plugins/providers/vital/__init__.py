"""The Vital provider: semantics, registry data, codec, depth and rule table.

Isolation rules this package must keep (AUD-005, enforced by
`tests/plugins/test_p7_isolation.py`):

- no import of `reapy` / `reascript_api` / `ReaperController`;
- no import of the legacy `_b4_lib` / `_bh_session` chain (the codec is a port of
  the functions that module was verified to keep host-free, not an import of it);
- importing this package must not read a registry file, connect to anything or
  change the working directory - the registry is read on first use.
"""

from __future__ import annotations

from .codec import VitalCodec
from .profile_build import (
    DEFAULT_REGISTRY_DIR,
    REGISTRY_DIR_ENV,
    SUPPORTED_VERSIONS,
    VitalRegistry,
    build_profile,
    load_registry,
    resolve_registry_dir,
)
from .provider import IMPLEMENTATION_REVISION, VitalProvider

__all__ = [
    "VitalProvider",
    "VitalCodec",
    "VitalRegistry",
    "build_profile",
    "load_registry",
    "resolve_registry_dir",
    "DEFAULT_REGISTRY_DIR",
    "REGISTRY_DIR_ENV",
    "SUPPORTED_VERSIONS",
    "IMPLEMENTATION_REVISION",
]
