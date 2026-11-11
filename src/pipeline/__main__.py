"""Process entrypoint: Telethon listener + approval bot + worker, one loop.

Ollama is expected on the *host*, not in Docker — Docker Desktop cannot reach
the GPU. From inside a container set ``OLLAMA_HOST=http://host.docker.internal:11434``.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
from pathlib import Path

from .backup import BACKUP_FIRST_RUN_S, backup_job
from .config import Settings
from .db import Database
from .faults import ig_alerts, search_alerts
from .digest import make_failure_notifier, send_digest
from .lease import LEASE_HEARTBEAT_S, LeaseHeld, WorkerLease
from .llm import LLMClient
from .logredact import install as install_redaction
from .models import WORKER_HALTS, Status
from .publish.instagram import publish_carousel
from .publish.media_host import upload
from .publish.theme_guard import ensure_distinct_theme
from .publish.tokens import TokenStore, refresh_token_if_due
from .publish.tunnel import clear_origin as clear_tunnel_origin
from .publish.tunnel import supervise as supervise_tunnel
from .retention import retention_job
from .safe_download import RoutingTransport
from .stages.compose import compose
from .stages.extract import extract
from .stages.render import render
from .stages.research import research
from .stages.revise import revise
from .stages.synthesize import synthesize
from .stages.triage import triage
from .storage import UnsafeStorage, require_local_fs
from .worker import Worker

log = logging.getLogger("pipeline")

#: The app writes here itself; launchd's stdout goes elsewhere so the two
#: never share a file descriptor.
LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "pipeline.log"
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_BACKUPS = 5

DIGEST_INTERVAL_S = 24 * 3600
TOKEN_CHECK_INTERVAL_S = 12 * 3600
#: First retention sweep shortly after boot, so a worker that restarts more
#: often than CLEANUP_INTERVAL_S still cleans up. The interval itself is config.
RETENTION_FIRST_RUN_S = 60


def build_stage_registry(*, db, llm, http, settings, bot, revise_notify=None) -> dict:
    """Map every worker-owned status to the stage that advances it.

    ``AWAITING_APPROVAL`` is deliberately absent: only the approval bot moves an
    item out of the human gate. ``revise_notify`` is the async ``notify(item, Notice)``
    the revise stage calls after its transition (the Yes/No, the "what do you mean?"
    and the deferral message); None sends nothing (tests, dry wiring).
    """
    async def operator_note(text: str) -> None:
        if bot is not None:
            await bot.send_message(settings.operator_user_id, text)

    async def guard_theme(item):
        return await ensure_distinct_theme(
            item, db=db, settings=settings, notify=operator_note,
        )

    async def do_publish(item):
        post_id = await publish_carousel(
            item, http, db, settings, before_publish=guard_theme,
        )
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
            partial_stage(
                triage, llm=llm, db=db, settings=settings,
                threshold=settings.triage_threshold,
            ),
            Status.TRIAGED,
        ),
        Status.TRIAGED: (
            partial_stage(research_or_enumerate, llm=llm, http=http, settings=settings),
            Status.RESEARCHED,
        ),
        Status.RESEARCHED: (
            # T108/T109: synthesize budgets its notes blobs against
            # num_ctx_good now, same as compose's own notes block — needs
            # settings for the same reason compose does, below.
            partial_stage(synthesize, llm=llm, settings=settings), Status.SYNTHESIZED,
        ),
        Status.SYNTHESIZED: (
            partial_stage(compose, llm=llm, settings=settings), Status.COMPOSED,
        ),
        Status.COMPOSED: (
            partial_stage(render, settings=settings, db=db), Status.RENDERED,
        ),
        Status.RENDERED: (
            partial_stage(send_preview_stage, bot=bot, settings=settings),
            Status.AWAITING_APPROVAL,
        ),
        # T301: the operator asked for a targeted edit. COMPOSED is the default next
        # status (slides/theme changed, re-render); the stage returns RENDERED for a
        # caption-only/no-change run, REVISION_CONFIRM / AWAITING_APPROVAL / SYNTHESIZED
        # for the confirm gate, "nothing grounded" and the flag-off rollback.
        Status.REVISION_REQUESTED: (
            partial_stage(
                revise, llm=llm, http=http, settings=settings, db=db, notify=revise_notify,
            ),
            Status.COMPOSED,
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


async def research_or_enumerate(item, llm, http, settings):
    """Route between verifying a claim and enumerating a set.

    Both produce research notes and land on RESEARCHED, so everything
    downstream is unchanged — only how the material is gathered differs.
    """
    from .stages.enumerate_items import enumerate_items

    plan = await enumerate_items(item, llm, http, settings)
    if plan.get("intent") == "list":
        return plan
    # Single-subject explainers ("what is X", a bare link) take this path too;
    # the classifier's clauses and spelled-out subject guide the planner.
    fields = await research(item, llm, http, settings, hints=plan)
    # Stored as "news": downstream only distinguishes "list" from everything else.
    fields["intent"] = "news"
    return fields


async def send_preview_stage(item, bot, settings):
    from .approval.bot import send_preview

    return await send_preview(bot, item, settings)


async def upload_stage(item, settings):
    urls = await upload(item.rendered_paths, item.id, settings)
    return {"media_urls": urls}


def missing_statuses(registry: dict) -> set:
    """Statuses no stage would ever advance. Any result but empty strands items."""
    return {s for s in Status if s not in WORKER_HALTS} - set(registry)


#: Wait between listener reconnection attempts.
LISTENER_RETRY_S = 30


async def supervise_listener(telethon, listener, stop: asyncio.Event) -> None:
    """Keep the channel listener alive for the life of the process.

    Telethon gives up after a handful of reconnection attempts. Without
    supervision the task simply ends, the process keeps running, and the
    pipeline is silently deaf to the channel — indistinguishable from a quiet
    channel until someone thinks to check.

    Every successful (re)connect re-runs backfill, so messages posted during an
    outage are recovered rather than lost.
    """
    while not stop.is_set():
        try:
            if not telethon.is_connected():
                await telethon.connect()
            recovered = await listener.backfill()
            if recovered:
                log.info("recovered %d messages missed while disconnected", recovered)
            await telethon.run_until_disconnected()
            if stop.is_set():
                return
            log.warning("channel listener disconnected")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel listener failed")

        log.warning("reconnecting channel listener in %ss", LISTENER_RETRY_S)
        try:
            await asyncio.wait_for(stop.wait(), timeout=LISTENER_RETRY_S)
            return  # stop was set while waiting
        except asyncio.TimeoutError:
            pass


async def _periodic(interval: float, coro_factory, stop: asyncio.Event,
                    first_delay: float | None = None) -> None:
    delay = interval if first_delay is None else first_delay
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        delay = interval
        try:
            await coro_factory()
        except Exception:
            log.exception("scheduled job failed")


async def main() -> int:
    # The app owns its log file rather than letting launchd redirect stdout
    # into it. A redirected file cannot be rotated — moving it leaves launchd
    # writing to the old inode — and pipeline.log had grown to 25,000 lines of
    # every run ever, which is how a DRY_RUN warning from eight days earlier
    # read as the current state.
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ]
    # Redact at the sink, before basicConfig hands the handlers over. httpx
    # logs full request URLs at INFO and Meta's read endpoints take the access
    # token as a query parameter, so a container-status GET put a live token
    # into the log in cleartext. See logredact for why this is a filter on
    # every handler rather than a fix at each call site.
    install_redaction(handlers)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    # trafilatura logs an unparseable page at ERROR, but fetch_text returns
    # None and every caller treats that as survivable — a dead or paywalled
    # link must not fail an item. Left at ERROR it puts lines like "empty HTML
    # tree" in the log next to real faults, and infrastructure noise reading as
    # a content failure has already sent debugging the wrong way more than once.
    for noisy in ("trafilatura.core", "trafilatura.utils", "trafilatura.metadata"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    # One INFO line per HTTP request buries the pipeline's own narration; the
    # stage logs already say what was called and what came back. Redaction
    # above is what protects the token — this only cuts noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    settings = Settings.load()
    settings.validate_for_run()

    if settings.dry_run:
        log.warning("DRY_RUN is on — previews will arrive, nothing will publish")

    # T348: one worker at a time, host or container. Taken before the database,
    # Telethon and the bot, so a second worker never touches any of them.
    lease = WorkerLease(settings.lease_path)
    try:
        # The Telethon session is a SQLite file too (D298); checked first so a
        # refusal leaves no lease behind.
        require_local_fs(settings.session_path, "the Telegram session")
        lease.acquire()
    except (LeaseHeld, UnsafeStorage) as exc:
        log.error("%s", exc)
        return 1
    # The heartbeat starts here, not with the other jobs: DB connect and
    # telethon.start() can take minutes (it retries forever offline), and a lease
    # that goes stale during startup would let a second worker acquire it.
    stop = asyncio.Event()

    async def heartbeat() -> None:
        # Fencing: if the lease no longer names this worker, stop the process.
        if not lease.heartbeat():
            stop.set()

    beat = asyncio.create_task(_periodic(LEASE_HEARTBEAT_S, heartbeat, stop))
    try:
        return await _run(settings, stop)
    finally:
        stop.set()
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        lease.release()


async def _run(settings: Settings, stop: asyncio.Event) -> int:
    import httpx
    from aiogram import Bot, Dispatcher
    from telethon import TelegramClient

    from .approval.bot import Pending, make_revision_notifier, register_approval
    from .intake.channel import ChannelListener

    db = await Database(settings.db_path, lease_path=settings.lease_path).connect()
    # T227: pages/images fetched from untrusted urls ask this client for the
    # pinned transport (public addresses only, resolved once at connect);
    # SearXNG / Ollama / Telegram traffic keeps the normal one.
    http = httpx.AsyncClient(follow_redirects=True, transport=RoutingTransport())
    llm = LLMClient(settings, http)
    bot = Bot(token=settings.telegram_bot_token)
    # T259: a config fault (missing model, dead slug) is alerted once per
    # process, to the operator, rather than failing items one by one.
    llm.on_config_fault = lambda text: bot.send_message(settings.operator_user_id, text)
    search_alerts.send = llm.on_config_fault  # same once-per-fault alert, search side
    ig_alerts.send = llm.on_config_fault  # T287: Instagram throttle, once per stall
    dispatcher = Dispatcher()
    pending = Pending()
    token_store = TokenStore(settings.db_path.parent / "ig_token.json")

    register_approval(dispatcher, db, settings, bot, pending)

    registry = build_stage_registry(
        db=db, llm=llm, http=http, settings=settings, bot=bot,
        revise_notify=make_revision_notifier(bot, db, settings, pending),
    )
    stranded = missing_statuses(registry)
    if stranded:
        raise RuntimeError(f"no stage registered for: {sorted(map(str, stranded))}")

    worker = Worker(
        db, registry, max_attempts=settings.max_attempts,
        on_failure=make_failure_notifier(bot, settings),
        bot=bot, settings=settings,
    )

    telethon = TelegramClient(
        str(settings.session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        # Telethon defaults to 5 attempts and then gives up for good. On a box
        # meant to run unattended for months, "give up" is never correct.
        connection_retries=None,
        retry_delay=5,
        auto_reconnect=True,
    )
    if stop.is_set():  # fenced while starting up: never bring Telethon up
        log.error("stop requested or lease lost during startup; not starting Telethon")
        await http.aclose()
        await db.close()
        return 1
    await telethon.start()
    if stop.is_set():
        log.error("stop requested or lease lost while Telethon started; shutting down")
        await http.aclose()
        await db.close()
        await telethon.disconnect()
        return 1
    listener = ChannelListener(db, telethon, settings)
    listener.register()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("running — watching %s", list(settings.channel_ids))

    tasks = [
        asyncio.create_task(worker.run_slow(settings.poll_interval_s, stop)),
        asyncio.create_task(worker.run_fast(stop=stop)),
        asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False)),
        asyncio.create_task(supervise_listener(telethon, listener, stop)),
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
        asyncio.create_task(_periodic(
            settings.cleanup_interval_s,
            lambda: retention_job(bot, db, settings),
            stop, first_delay=RETENTION_FIRST_RUN_S,
        )),
    ]
    if settings.backup_interval_s > 0:
        tasks.append(asyncio.create_task(_periodic(
            settings.backup_interval_s, lambda: backup_job(settings), stop,
            first_delay=BACKUP_FIRST_RUN_S,
        )))

    if settings.manage_tunnel:
        # In-process rather than its own launchd job: macOS blocks a newly
        # added background item until someone approves it in System Settings,
        # so a separate service exits 78 with no output until a human notices.
        tasks.append(asyncio.create_task(supervise_tunnel(stop, settings.tunnel_target)))
    else:
        # Nothing is managing the hostname, so whatever is configured is what
        # gets used. Fine for a real bucket; a lie for a quick tunnel.
        clear_tunnel_origin()

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
