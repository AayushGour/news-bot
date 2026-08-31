"""The only authorisation boundary in the system.

Telegram bot usernames are discoverable and anyone can start a conversation with
one. Two things make that dangerous here:

* DM intake bypasses triage entirely, so an unauthorised message is a direct,
  unfiltered path into the publishing pipeline.
* An unauthorised callback could approve a post to the operator's Instagram.

Every handler goes through :func:`operator_only`. Unauthorised events are
ignored in silence — no reply, no error, not even an acknowledgement that the
bot is alive.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


def is_operator(event: Any, operator_user_id: int) -> bool:
    """True only for a positively identified operator.

    A missing ``from_user``, a missing id, or an unset ``operator_user_id`` all
    resolve to False. There is no configuration under which this returns True
    by default.
    """
    if not operator_user_id:
        return False
    user = getattr(event, "from_user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    return user_id == operator_user_id


def operator_only(settings: Any) -> Callable:
    """Decorator enforcing :func:`is_operator` on a handler.

    Applied to every handler as a decorator rather than checked inline, so
    adding a new handler cannot silently omit the check.
    """

    def decorator(handler: Callable) -> Callable:
        @functools.wraps(handler)
        async def wrapper(event: Any, *args: Any, **kwargs: Any) -> Any:
            if not is_operator(event, settings.operator_user_id):
                user = getattr(event, "from_user", None)
                log.warning(
                    "ignored %s from unauthorised user %s",
                    handler.__name__, getattr(user, "id", "unknown"),
                )
                return None
            return await handler(event, *args, **kwargs)

        wrapper.__operator_only__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorator
