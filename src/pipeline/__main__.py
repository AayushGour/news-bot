"""Process entrypoint: Telethon listener + approval bot + worker, one loop.

Ollama is expected on the *host*, not in Docker — Docker Desktop cannot reach
the GPU. From inside a container set ``OLLAMA_HOST=http://host.docker.internal:11434``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from functools import partial

from .config import Settings
from .db import Database
from .digest import make_failure_notifier, send_digest
from .llm import LLMClient
from .models import WORKER_HALTS, Status
from .publish.instagram import publish_carousel
from .publish.media_host import upload
from .publish.tokens import TokenStore, refresh_token_if_due
from .stages.compose import compose
from .stages.extract import extract
from .stages.render import render
from .stages.research import research
from .stages.synthesize import synthesize
from .stages.triage import triage
from .worker import Worker

log = logging.getLogger("pipeline")

DIGEST_INTERVAL_S = 24 * 3600
TOKEN_CHECK_INTERVAL_S = 12 * 3600


def build_stage_registry(*, db, llm, http, settings, bot) -> dict:
    """Map every worker-owned status to the stage that advances it.

    ``AWAITING_APPROVAL`` is deliberately absent: only the approval bot moves an
    item out of the human gate.
    """
    from .approval.bot import send_preview

    async def do_publish(item):
        post_id = await publish_carousel(item, http, db, settings)
        from .db import now_iso

        return {"ig_post_id": post_id, "published_at": now_iso()}

    return {
        # Extraction runs BEFORE triage. Many channel posts are an image with
        # no caption; triaging on body text alone drops those unread and
        # reports it as a quiet channel. Costs one vision call per post ahead
        # of the filter, which is the price of not being blind.
        Status.INGESTED: (
            partial_stage(extract, llm=llm, http=http), Status.EXTRACTED,
        ),
        Status.EXTRACTED: (
            partial_stage(triage, llm=llm, db=db, threshold=settings.triage_threshold),
            Status.TRIAGED,
        ),
        Status.TRIAGED: (
            partial_stage(research, llm=llm, http=http, settings=settings),
            Status.RESEARCHED,
        ),
        Status.RESEARCHED: (
            partial_stage(synthesize, llm=llm), Status.SYNTHESIZED,
        ),
        Status.SYNTHESIZED: (
            partial_stage(compose, llm=llm, settings=settings), Status.COMPOSED,
        ),
        Status.COMPOSED: (
            partial_stage(render, settings=settings), Status.RENDERED,
        ),
        Status.RENDERED: (
            partial_stage(send_preview_stage, bot=bot, settings=settings),
            Status.AWAITING_APPROVAL,
        ),
        Status.APPROVED: (
            partial_stage(upload_stage, settings=settings), Status.PUBLISHING,
        ),
        Status.PUBLISHING: (do_publish, Status.PUBLISHED),
    }


def partial_stage(func, **kwargs):
    """Bind dependencies, leaving ``item`` as the only call argument."""

    async def stage(item):
        return await func(item, **kwargs)

    stage.__name__ = getattr(func, "__name__", "stage")
    return stage


async def send_preview_stage(item, bot, settings):
    from .approval.bot import send_preview

    return await send_preview(bot, item, settings)


async def upload_stage(item, settings):
    urls = await upload(item.rendered_paths, item.id, settings)
    return {"media_urls": urls}


def missing_statuses(registry: dict) -> set:
    """Statuses no stage would ever advance. Any result but empty strands items."""
    return {s for s in Status if s not in WORKER_HALTS} - set(registry)


async def _periodic(interval: float, coro_factory, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await coro_factory()
        except Exception:
            log.exception("scheduled job failed")


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    settings = Settings.load()
    settings.validate_for_run()

    if settings.dry_run:
        log.warning("DRY_RUN is on — previews will arrive, nothing will publish")

    import httpx
    from aiogram import Bot, Dispatcher
    from telethon import TelegramClient

    from .approval.bot import Pending, register_approval
    from .intake.channel import ChannelListener

    db = await Database(settings.db_path).connect()
    http = httpx.AsyncClient(follow_redirects=True)
    llm = LLMClient(settings, http)
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    pending = Pending()
    token_store = TokenStore(settings.db_path.parent / "ig_token.json")

    register_approval(dispatcher, db, settings, bot, pending)

    registry = build_stage_registry(
        db=db, llm=llm, http=http, settings=settings, bot=bot
    )
    stranded = missing_statuses(registry)
    if stranded:
        raise RuntimeError(f"no stage registered for: {sorted(map(str, stranded))}")

    worker = Worker(
        db, registry, max_attempts=settings.max_attempts,
        on_failure=make_failure_notifier(bot, settings),
    )

    telethon = TelegramClient(
        str(settings.session_path), settings.telegram_api_id, settings.telegram_api_hash
    )
    await telethon.start()
    listener = ChannelListener(db, telethon, settings)
    listener.register()
    await listener.backfill()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("running — watching %s", list(settings.channel_ids))

    tasks = [
        asyncio.create_task(worker.run(settings.poll_interval_s, stop)),
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False)),
        asyncio.create_task(telethon.run_until_disconnected()),
        asyncio.create_task(_periodic(
            DIGEST_INTERVAL_S, lambda: send_digest(bot, db, settings), stop)),
        asyncio.create_task(_periodic(
            TOKEN_CHECK_INTERVAL_S,
            lambda: refresh_token_if_due(
                http, settings, token_store,
                notify=lambda t: bot.send_message(settings.operator_user_id, t),
            ),
            stop,
        )),
    ]

    await stop.wait()
    log.info("shutting down")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await http.aclose()
    await db.close()
    await telethon.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
