"""Provider discovery by id: the one place a configured id meets a provider class.

Core may not contain a provider identifier (08 §8.1: the L0 boundary check bans
them outside `providers/`, comments included), so the mapping from a configured
provider id to its implementation lives here, inside the provider package. Core
asks for a provider by id and gets either one or a typed `UnsupportedProvider`.

Nothing is imported at import time: the provider module is loaded inside the
factory, on first use, so importing this module stays side-effect free (P0-1,
P7-1).
"""

from __future__ import annotations

from typing import Any, Callable

from ..errors import UnsupportedProvider

__all__ = ["PROVIDER_IDS", "known_provider_ids", "load_provider"]


def _make_vital() -> Any:
    from .vital import VitalProvider

    return VitalProvider()


#: id -> factory. The ids are the configuration vocabulary of
#: `REAPER_MCP_PLUGIN_PROVIDERS` (13 §4).
_FACTORIES: dict[str, Callable[[], Any]] = {
    "vital": _make_vital,
}

#: The ids a caller may configure, for refusal details.
PROVIDER_IDS: tuple[str, ...] = tuple(sorted(_FACTORIES))


def known_provider_ids() -> tuple[str, ...]:
    return PROVIDER_IDS


def load_provider(provider_id: str, *, config: Any = None) -> Any:
    """The provider instance for this id, or a typed refusal naming the known ids.

    An unknown id is never silently ignored: a configuration typo must fail loudly
    and answer `UnsupportedProvider` with the accepted vocabulary.
    """
    key = str(provider_id).strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        raise UnsupportedProvider(
            f"no provider is known by the id {provider_id!r}",
            detail={"provider": provider_id, "known_providers": list(PROVIDER_IDS)},
        )
    return factory()
