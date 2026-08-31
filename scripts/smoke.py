"""End-to-end smoke test against real Ollama and SearXNG. No Telegram, no Instagram.

Runs one item through triage -> extract -> research -> synthesize -> compose ->
render and prints what each stage produced, so you can see the real output
before wiring up any accounts.

    docker compose up -d searxng
    ./.venv/bin/python scripts/smoke.py
    ./.venv/bin/python scripts/smoke.py --text "your own news item"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.llm import LLMClient  # noqa: E402
from pipeline.models import Status  # noqa: E402
from pipeline.stages.compose import compose  # noqa: E402
from pipeline.stages.extract import extract  # noqa: E402
from pipeline.stages.render import render  # noqa: E402
from pipeline.stages.research import research  # noqa: E402
from pipeline.stages.synthesize import synthesize  # noqa: E402
from pipeline.stages.triage import triage  # noqa: E402

DEFAULT_TEXT = """Cursor's leadership has responded to OpenAI's announcement that it will block Cursor users from accessing OpenAI models within the next three months.

According to Cursor, OpenAI models account for approximately five percent of Cursor's user traffic. Cursor representatives stated that they are currently in discussions with OpenAI to address the situation.

The company further noted that Cursor was among the early adopters of OpenAI's technologies and had relied on OpenAI's platform as neutral infrastructure for its business."""


async def preflight(http: httpx.AsyncClient, settings: Settings) -> bool:
    ok = True
    try:
        r = await http.get(f"{settings.ollama_host}/api/tags", timeout=10)
        names = {m["name"] for m in r.json().get("models", [])}
        for role, model in [("cheap", settings.model_cheap), ("good", settings.model_good)]:
            mark = "OK " if model in names else "MISSING"
            print(f"  {mark:8} {role:6} {model}")
            ok &= model in names
    except Exception as exc:
        print(f"  MISSING  ollama at {settings.ollama_host}: {exc}")
        ok = False

    try:
        r = await http.get(f"{settings.searxng_url}/search",
                           params={"q": "test", "format": "json"}, timeout=10)
        good = r.status_code == 200
        print(f"  {'OK ' if good else 'MISSING':8} searxng {settings.searxng_url}")
        ok &= good
    except Exception as exc:
        print(f"  MISSING  searxng at {settings.searxng_url}: {exc}")
        ok = False
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--source", default="dm", choices=["dm", "channel"],
                        help="'dm' skips triage, 'channel' exercises it")
    args = parser.parse_args()

    settings = Settings.load(env={
        "DB_PATH": str(ROOT / "data" / "smoke.db"),
        "SOURCE_CREDIT": "@aipost",
    })

    print("preflight:")
    async with httpx.AsyncClient(follow_redirects=True) as http:
        if not await preflight(http, settings):
            print("\nFix the above first. `docker compose up -d searxng`, "
                  "`ollama serve`, `ollama pull ...`")
            return 1

        db = await Database(settings.db_path).connect()
        llm = LLMClient(settings, http)

        item_id = await db.insert_item(
            source=args.source, source_chat_id=-1,
            source_msg_id=int(time.time()), raw_text=args.text,
        )
        timings: list[tuple[str, float]] = []

        async def run(name, coro_factory, next_status):
            item = await db.get_item(item_id)
            t0 = time.perf_counter()
            print(f"\n▶ {name} ...", flush=True)
            fields = await coro_factory(item)
            elapsed = time.perf_counter() - t0
            timings.append((name, elapsed))
            target = fields.pop("_next", next_status)
            await db.transition(item_id, target, fields)
            print(f"✔ {name}: {elapsed:.1f}s -> {target}")
            return await db.get_item(item_id)

        item = await run("triage", lambda i: triage(i, llm, db, settings.triage_threshold),
                         Status.TRIAGED)
        print(f"   score {item.triage_score}: {item.triage_reason}")
        if item.status == Status.DROPPED:
            print("\nDropped at triage — nothing further to do.")
            await db.close()
            return 0

        await run("extract", lambda i: extract(i, llm, http), Status.EXTRACTED)
        item = await run("research", lambda i: research(i, llm, http, settings),
                         Status.RESEARCHED)
        for note in item.research:
            print(f"   · {note['claim'][:80]}")
            for url in note["sources"]:
                print(f"       {url}")

        item = await run("synthesize", lambda i: synthesize(i, llm), Status.SYNTHESIZED)
        print("\n" + item.brief + "\n")

        item = await run("compose", lambda i: compose(i, llm, settings), Status.COMPOSED)
        print(f"   {len(item.slides)} slides: {[s['type'] for s in item.slides]}")
        print(f"   caption:\n{item.caption}\n")

        item = await run("render", lambda i: render(i, settings), Status.RENDERED)
        for path in item.rendered_paths:
            print(f"   {path}")

        (ROOT / "data" / "smoke_result.json").write_text(
            json.dumps({"brief": item.brief, "slides": item.slides,
                        "caption": item.caption, "research": item.research}, indent=2),
            encoding="utf-8",
        )

        print("\ntimings:")
        for name, elapsed in timings:
            print(f"  {name:12} {elapsed:7.1f}s")
        print(f"  {'TOTAL':12} {sum(t for _, t in timings):7.1f}s")

        domains = item.source_domains
        print(f"\nsource domains used: {', '.join(domains) if domains else 'NONE'}")
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
