"""Compilers: turn an external description of a project into the domain model."""

from __future__ import annotations

from .audiocpp import from_manifest

__all__ = ["from_manifest"]
