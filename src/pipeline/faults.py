"""Alert the operator once per distinct config fault (T259, T260 C2).

Every queued item hits the same missing model or disabled SearXNG format, and
an alert per item would bury the one that matters. One mechanism, shared by the
LLM client and the search path, so the once-per-key rule has a single copy.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .errors import ConfigFault

log = logging.getLogger(__name__)


class OnceAlerter:
    """Builds a ConfigFault, alerting via ``send`` once per ``key``."""

    def __init__(self) -> None:
        #: Async ``(message) -> None`` the entrypoint wires to the operator's chat.
        self.send: Callable[[str], Awaitable[None]] | None = None
        self._seen: set[str] = set()

    async def fault(self, key: str, message: str) -> ConfigFault:
        """Once per fault per process, not per item. Never raises: an alert
        failure must not mask the fault itself."""
        if key not in self._seen:
            self._seen.add(key)
            log.error("CONFIG FAULT (alerting once): %s", message)
            if self.send is not None:
                try:
                    await self.send(f"Config fault: {message}")
                except Exception:
                    log.exception("config-fault alert failed to send")
        return ConfigFault(message)


    async def notify_once(self, key: str, message: str) -> None:
        """Send a plain operator message once per ``key`` until ``reset(key)``.
        Never raises."""
        if key in self._seen:
            return
        self._seen.add(key)
        if self.send is not None:
            try:
                await self.send(message)
            except Exception:
                log.exception("operator alert failed to send")

    def reset(self, key: str) -> None:
        """Re-arm ``key`` (the stall ended), so the next one alerts again."""
        self._seen.discard(key)


#: The search path's alerter (the LLM client owns its own instance).
search_alerts = OnceAlerter()
#: Instagram throttle alerts (T287): one per stall, re-armed on a good publish.
ig_alerts = OnceAlerter()
