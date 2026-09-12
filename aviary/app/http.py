"""One registry for the outbound httpx clients.

Eight modules each talk to something over HTTP — Frigate and BirdNET-Go (``proxy``),
Wikipedia and iNaturalist (``species_info``, ``species_photos``, ``species_audio``,
``seasonality``, ``inat``), the Supervisor (``solar``, ``notify``) and the aviary-id
service (``identify``) — and each used to carry the same ``_client`` global with an
``init_client`` / ``close_client`` pair that ``main.py`` wired by hand, twice. Adding a
ninth meant touching three places and remembering the shutdown order.

Each module now registers ONE handle at import time, keeping its own construction (the
timeouts and headers differ on purpose: 8 s for a reference lookup, no read deadline for
a clip download), and the lifespan calls ``init_all()`` / ``close_all()`` once. A handle
is ``None`` until initialised, exactly like the old globals, so every ``if client is
None`` early return keeps its meaning. ``on_close`` covers the one module with state tied
to its client (the BirdNET-Go CSRF token lives in the client's cookie jar).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import httpx

log = logging.getLogger("aviary.http")


class Handle:
    """A lazily constructed ``httpx.AsyncClient`` owned by one module."""

    def __init__(self, name: str, factory: Callable[[], Optional[httpx.AsyncClient]],
                 on_close: Optional[Callable[[], None]] = None) -> None:
        self.name = name
        self._factory = factory
        self._on_close = on_close
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> Optional[httpx.AsyncClient]:
        return self._client

    def init(self) -> None:
        """Build the client once. A factory returning None leaves the handle unset (a
        module whose settings rule it out); calling again is a no-op."""
        if self._client is None:
            self._client = self._factory()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._on_close is not None:
            self._on_close()


_handles: list[Handle] = []


def register(name: str, factory: Optional[Callable[[], Optional[httpx.AsyncClient]]] = None,
             *, on_close: Optional[Callable[[], None]] = None, **client_kwargs) -> Handle:
    """Declare a module's client. Pass ``factory`` when construction depends on runtime
    settings; otherwise ``client_kwargs`` go straight to ``httpx.AsyncClient``."""
    if factory is None:
        def factory() -> httpx.AsyncClient:  # noqa: E306 - closure over client_kwargs
            return httpx.AsyncClient(**client_kwargs)
    handle = Handle(name, factory, on_close)
    _handles.append(handle)
    return handle


def init_all() -> None:
    for handle in _handles:
        handle.init()


async def close_all() -> None:
    """Close in reverse registration order; a failing close never blocks the others."""
    for handle in reversed(_handles):
        try:
            await handle.close()
        except Exception:  # noqa: BLE001
            log.exception("Closing the %s HTTP client failed", handle.name)
