"""Verify every dependency and credential before a first real run.

Checks live services, not just that .env is non-empty: a token that parses but
was revoked looks identical to a working one until the first send fails.

Secrets are never printed — only whether they work.

    ./.venv/bin/python scripts/preflight.py
    ./.venv/bin/python scripts/preflight.py --send   # also DMs you a test message
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from pipeline.config import MissingConfig, Settings  # noqa: E402

OK, BAD, WARN = "  OK   ", " FAIL  ", " WARN  "
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
results: list[tuple[str, str]] = []


def record(status: str, label: str, detail: str = "") -> None:
    results.append((status, label))
    print(f"{status} {label}" + (f"\n         {detail}" if detail else ""), flush=True)


def mask(value: str) -> str:
    """Enough to identify a value, not enough to use it."""
    if not value:
        return "(empty)"
    return f"{value[:4]}…{value[-2:]} ({len(value)} chars)"


def check_imports() -> None:
    """Every third-party module the process needs at runtime.

    Several are imported lazily inside main(), so a missing one surfaces only
    after startup logging has already claimed success — which is exactly how
    aiogram went unnoticed until the first live launch.
    """
    import importlib

    for module, why in [
        ("aiogram", "approval bot"),
        ("telethon", "channel listener"),
        ("httpx", "all HTTP"),
        ("trafilatura", "article extraction"),
        ("jinja2", "slide templates"),
        ("playwright", "rendering"),
        ("boto3", "R2 upload"),
        ("aiosqlite", "database"),
    ]:
        try:
            importlib.import_module(module)
        except ImportError:
            record(BAD, f"import {module} ({why})",
                   'run: ./.venv/bin/pip install -e ".[dev]"')
            return
    record(OK, "imports: all runtime dependencies present")


async def check_config() -> Settings | None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        record(WARN, "python-dotenv missing", "reading process env only")

    settings = Settings.load()
    try:
        settings.validate_for_run()
        record(OK, "config: all required variables present")
    except MissingConfig as exc:
        record(BAD, "config incomplete", str(exc))
        return None

    record(OK, f"config: watching channels {list(settings.channel_ids)}")
    record(OK, f"config: LLM_PROVIDER={settings.llm_provider}")
    record(
        OK if settings.dry_run else WARN,
        f"config: DRY_RUN={settings.dry_run}",
        "" if settings.dry_run else "THIS WILL PUBLISH TO INSTAGRAM FOR REAL",
    )
    return settings


async def check_bot(http: httpx.AsyncClient, settings: Settings, send: bool) -> None:
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        response = await http.get(f"{base}/getMe", timeout=20)
        body = response.json()
    except Exception as exc:
        record(BAD, "bot token: cannot reach Telegram", str(exc))
        return

    if not body.get("ok"):
        record(BAD, "bot token rejected", str(body.get("description", body))[:200])
        return

    bot = body["result"]
    record(OK, f"bot token valid: @{bot.get('username')} ({bot.get('first_name')})")

    if not send:
        record(WARN, "operator id unverified", "re-run with --send to prove delivery")
        return

    try:
        response = await http.post(
            f"{base}/sendMessage",
            json={
                "chat_id": settings.operator_user_id,
                "text": (
                    "✅ Preflight OK.\n\n"
                    "Your bot can reach you, so approval previews will arrive here.\n"
                    f"Watching: {list(settings.channel_ids)}\n"
                    f"DRY_RUN: {settings.dry_run}"
                ),
            },
            timeout=20,
        )
        body = response.json()
    except Exception as exc:
        record(BAD, "test message failed", str(exc))
        return

    if body.get("ok"):
        record(OK, f"operator id {settings.operator_user_id} reachable — check Telegram")
    else:
        description = str(body.get("description", ""))
        hint = ""
        if "chat not found" in description.lower():
            hint = ("Open a chat with the bot and press Start first — Telegram "
                    "forbids bots from messaging users who never contacted them.")
        record(BAD, "cannot DM operator", f"{description}\n         {hint}".rstrip())


async def check_session(settings: Settings) -> None:
    path = Path(settings.session_path)
    if not path.exists():
        record(
            WARN, "telethon session missing",
            f"{path}\n         Channel reading needs this. See README "
            f"'Generate the Telegram session'. DM intake works without it.",
        )
        return

    mode = oct(path.stat().st_mode)[-3:]
    record(OK, f"telethon session present ({path.stat().st_size} bytes, mode {mode})")
    if mode != "600":
        record(WARN, "session file permissions",
               "This file is account-equivalent. Run: chmod 600 " + str(path))


async def check_ollama(http: httpx.AsyncClient, settings: Settings) -> None:
    try:
        response = await http.get(f"{settings.ollama_host}/api/tags", timeout=15)
        installed = {m["name"] for m in response.json().get("models", [])}
    except Exception as exc:
        record(BAD, f"ollama unreachable at {settings.ollama_host}", str(exc))
        return

    record(OK, f"ollama up ({len(installed)} models)")
    for role, model in [
        ("cheap ", settings.model_cheap),
        ("good  ", settings.model_good),
        ("vision", settings.model_vision),
    ]:
        if model in installed:
            record(OK, f"model {role} {model}")
        else:
            record(BAD, f"model {role} {model} NOT INSTALLED",
                   f"run: ollama pull {model}")


async def check_openrouter(http: httpx.AsyncClient, settings: Settings) -> None:
    """Prove the key is live and the three configured models are routable.

    The key itself is never printed — only whether OpenRouter accepted it.
    A key that parses but was revoked looks identical to a working one until
    the first item fails, and on the hosted path that failure is Terminal.
    """
    if not settings.openrouter_api_key:
        record(BAD, "openrouter: OPENROUTER_API_KEY not set",
               "LLM_PROVIDER=openrouter needs a key from https://openrouter.ai/keys")
        return

    try:
        response = await http.get(
            f"{OPENROUTER_BASE}/key",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=20,
        )
    except Exception as exc:
        record(BAD, "openrouter unreachable", str(exc))
        return

    if response.status_code in (401, 403):
        record(BAD, "openrouter key rejected",
               f"HTTP {response.status_code} — regenerate at https://openrouter.ai/keys")
        return
    if response.status_code != 200:
        record(WARN, f"openrouter key check inconclusive (HTTP {response.status_code})")
    else:
        data = response.json().get("data", {}) or {}
        usage, limit = data.get("usage"), data.get("limit")
        record(OK, f"openrouter key accepted (label: {data.get('label') or 'unnamed'})",
               f"usage {usage}, limit {'none (pay as you go)' if limit is None else limit}")
        if limit is not None and usage is not None and usage >= limit:
            record(WARN, "openrouter credit exhausted",
                   "calls will 402, which the pipeline treats as Terminal")

    try:
        response = await http.get(f"{OPENROUTER_BASE}/models", timeout=30)
        catalogue = {model["id"]: model for model in response.json().get("data", [])}
    except Exception as exc:
        record(WARN, "openrouter model list unavailable", str(exc))
        return

    record(OK, f"openrouter up ({len(catalogue)} models listed)")
    for role, model in [
        ("cheap ", settings.openrouter_model_cheap),
        ("good  ", settings.openrouter_model_good),
        ("vision", settings.openrouter_model_vision),
    ]:
        entry = catalogue.get(model)
        if entry is None:
            record(BAD, f"model {role} {model} NOT AVAILABLE",
                   "check the exact id at https://openrouter.ai/models")
            continue

        # The ':free' suffix config enforces is a naming convention; this is the
        # actual price. Checking it here catches a slug that looks free, and a
        # model that stops being free later without its name changing.
        pricing = entry.get("pricing") or {}
        try:
            cost = sum(float(pricing.get(k) or 0) for k in ("prompt", "completion"))
        except (TypeError, ValueError):
            record(WARN, f"model {role} {model} price unreadable", str(pricing))
            continue

        if cost > 0:
            record(BAD, f"model {role} {model} IS PAID",
                   f"prompt={pricing.get('prompt')} completion={pricing.get('completion')}"
                   " — only free models are allowed")
        else:
            record(OK, f"model {role} {model}", "free")


async def check_searxng(http: httpx.AsyncClient, settings: Settings) -> None:
    try:
        response = await http.get(
            f"{settings.searxng_url}/search",
            params={"q": "openai", "format": "json"}, timeout=20,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        count = len(response.json().get("results", []))
    except Exception as exc:
        record(BAD, f"searxng unreachable at {settings.searxng_url}",
               f"{exc}\n         run: docker compose up -d searxng")
        return
    record(OK if count else WARN, f"searxng up ({count} results for a test query)")


async def check_publish(settings: Settings) -> None:
    if settings.dry_run:
        record(OK, "publishing: DRY_RUN on, R2/Instagram not required yet")
        return
    for name, value in [
        ("R2_BUCKET", settings.r2_bucket),
        ("R2_PUBLIC_BASE", settings.r2_public_base),
        ("IG_USER_ID", settings.ig_user_id),
        ("IG_ACCESS_TOKEN", settings.ig_access_token),
    ]:
        record(OK if value else BAD, f"publish: {name} {mask(value)}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true",
                        help="send a test DM to prove the approval path works")
    args = parser.parse_args()

    print("=" * 62)
    check_imports()
    settings = await check_config()
    if settings is None:
        return 1

    async with httpx.AsyncClient(follow_redirects=True) as http:
        print("-" * 62)
        await check_bot(http, settings, args.send)
        await check_session(settings)
        print("-" * 62)
        if settings.llm_provider == "openrouter":
            await check_openrouter(http, settings)
        else:
            await check_ollama(http, settings)
        await check_searxng(http, settings)
        print("-" * 62)
        await check_publish(settings)

    print("=" * 62)
    failed = sum(1 for status, _ in results if status == BAD)
    warned = sum(1 for status, _ in results if status == WARN)
    print(f"{len(results) - failed - warned} ok, {warned} warnings, {failed} failures")
    if failed:
        print("\nFailures above must be fixed before `python -m pipeline` will work.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
