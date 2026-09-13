"""The stage-advancing loop.

The worker is deliberately small. It knows how to pick items that are due, run
one stage, classify whatever went wrong, and commit. It knows nothing about
what any stage actually does.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

from .conversation import NeedsInput, ask
from .db import Database
from .errors import Recompose, Retryable, Retryforever, Terminal
from .models import WORKER_HALTS, Item, Status

log = logging.getLogger(__name__)

StageHandler = Callable[[Item], Awaitable[dict] | dict]
StageRegistry = dict[Status, tuple[StageHandler, Status]]

#: How long to wait before retrying when the infrastructure itself is down.
INFRA_BACKOFF_S = 60


class Worker:
    def __init__(
        self,
        db: Database,
        stages: StageRegistry,
        *,
        max_attempts: int = 3,
        batch: int = 3,
        on_failure: Callable[[Item, str], Awaitable[None]] | None = None,
        bot: Any = None,
        settings: Any = None,
    ) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings
        self.max_attempts = max_attempts
        self.batch = batch
        self.on_failure = on_failure
        # Defensive: even if a caller hands us a registry containing
        # AWAITING_APPROVAL, the worker refuses to act on it. The human gate is
        # not something a registry mistake should be able to open.
        self.stages: StageRegistry = {
            status: pair for status, pair in stages.items() if status not in WORKER_HALTS
        }

    #: Stages that only move bytes and call one API. They are fast, and they
    #: are the last thing standing between an operator pressing approve and a
    #: post appearing — so they get their own loop. Sharing one with research
    #: meant a single compose call, which the free tier has stretched to 983
    #: seconds, blocked every approval behind it: tick() waits for its whole
    #: batch before it can claim again.
    FAST_STAGES: frozenset = frozenset({Status.APPROVED, Status.PUBLISHING})

    async def tick(self, only=None) -> int:
        """Advance every currently-due item by one stage. Returns how many ran."""
        wanted = [s for s in self.stages if s in only] if only else list(self.stages)
        if not wanted:
            return 0
        items = await self.db.claim_items(wanted, limit=self.batch)
        if not items:
            return 0
        results = await asyncio.gather(
            *(self._run_one(item) for item in items), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):  # pragma: no cover - defensive
                log.exception("worker._run_one escaped", exc_info=result)
        return len(items)

    async def run(self, interval: float = 5.0, stop: asyncio.Event | None = None,
                  only=None) -> None:
        while stop is None or not stop.is_set():
            try:
                advanced = await self.tick(only)
            except Exception:  # pragma: no cover - loop must never die
                log.exception("worker tick failed")
                advanced = 0
            if not advanced:
                await asyncio.sleep(interval)

    async def run_slow(self, interval: float = 5.0,
                       stop: asyncio.Event | None = None) -> None:
        """Everything except publishing."""
        await self.run(interval, stop,
                       only=frozenset(self.stages) - self.FAST_STAGES)

    async def run_fast(self, interval: float = 2.0,
                       stop: asyncio.Event | None = None) -> None:
        """Upload and publish only, so approvals never queue behind research."""
        await self.run(interval, stop, only=self.FAST_STAGES)

    # ------------------------------------------------------------- internals

    async def _run_one(self, item: Item) -> None:
        handler, default_next = self.stages[item.status]
        try:
            result = handler(item)
            if inspect.isawaitable(result):
                result = await result
            fields: dict[str, Any] = dict(result or {})
            next_status = fields.pop("_next", default_next)
            await self.db.transition(item.id, Status(next_status), fields)

        except NeedsInput as exc:
            # The stage cannot do its job honestly. Park the item and put the
            # question to the operator rather than generating a thin post that
            # costs them a review and looks like the system working.
            log.info("item %s needs operator input: %s", item.id, exc.question[:80])
            await ask(
                self.db, self.bot, self.settings, item,
                exc.question, exc.resume_status, exc.confidence, exc.fields,
            )

        except Recompose as exc:
            # Rendering did not fit. Send it back for tighter copy rather than
            # failing the item — this is a normal, expected outcome.
            #
            # SYNTHESIZED, not COMPOSED: COMPOSED re-runs render, which would
            # produce the identical overflowing slides and bounce again, with
            # attempts reset each time. That is an unbounded loop.
            log.info("item %s recompose: %s", item.id, exc)
            await self.db.transition(
                item.id, Status.SYNTHESIZED,
                {"regen_note": f"Slide {exc.slide_index + 1} did not fit: {exc.reason}. "
                               f"Shorten it."},
                detail=str(exc),
            )

        except Retryforever as exc:
            # Infrastructure down. Not this item's fault — do not spend an attempt.
            log.warning("item %s deferred: %s", item.id, exc)
            await self.db.defer(item.id, INFRA_BACKOFF_S, str(exc))

        except Terminal as exc:
            log.error("item %s terminal: %s", item.id, exc)
            await self.db.record_failure(item.id, str(exc), terminal=True)
            await self._notify(item, str(exc))

        except Exception as exc:
            # Anything unclassified is assumed transient.
            reason = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, Retryable):
                log.exception("item %s unclassified failure", item.id)
            status = await self.db.record_failure(
                item.id, reason, max_attempts=self.max_attempts
            )
            if status == Status.FAILED:
                await self._notify(item, reason)

    async def _notify(self, item: Item, reason: str) -> None:
        if self.on_failure is None:
            return
        try:
            await self.on_failure(item, reason)
        except Exception:  # pragma: no cover - alerting must not cascade
            log.exception("failure notification failed for item %s", item.id)
