"""Score the compose stage against real items, so a prompt change is testable.

Every prompt change so far has landed on a single observation: one bad deck
seen, one thing changed, the next deck looked at. With a model in the loop
that is how you convince yourself something improved when it did not.

Everything here is measured mechanically. No model judges another model's
output — a judge would share the same blind spots and give a number that feels
like evidence without being any.

    ./.venv/bin/python scripts/eval_compose.py                 # score current prompt
    ./.venv/bin/python scripts/eval_compose.py --tag baseline  # save for comparison
    ./.venv/bin/python scripts/eval_compose.py --compare baseline fewshot
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from pipeline.config import Settings  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.llm import LLMClient  # noqa: E402
from pipeline.stages.compose import (  # noqa: E402
    MAX_HASHTAGS,
    MAX_SLIDES,
    MIN_SLIDES,
    compose,
)

RESULTS = ROOT / "data" / "eval"
GOLDEN = RESULTS / "golden.json"

#: Per-type headline limits as stated in the prompt. Measuring how often the
#: model exceeds what it was told is the point.
HEADLINE_LIMITS = {
    "hook": 60, "point": 45, "facts": 45, "code": 45, "flow": 45,
    "compare": 45, "quote": 40, "photo": 45, "kpi": 45, "chart": 45,
    "takeaway": 55, "sources": 45, "follow": 40, "links": 45, "repo": 60,
}

BODY_FIELDS = ("sub", "bullets", "stat", "rows", "urls", "code", "steps",
               "quote", "image", "tiles", "series", "links", "name")


def score_deck(item, doc: dict) -> dict:
    """Mechanical quality signals for one composed deck."""
    slides = doc.get("slides") or []
    types = [s.get("type") for s in slides]
    intent = item.intent or "news"

    empty = sum(1 for s in slides if not any(s.get(f) for f in BODY_FIELDS))
    overlong = sum(
        1 for s in slides
        if len(str(s.get("headline", ""))) > HEADLINE_LIMITS.get(s.get("type"), 60)
    )

    # Shape: a hook opens, and the closing slides are in the right order.
    #
    # The tail check must not pass vacuously. An empty tail sorts equal to
    # itself, so a deck with no closing slide at all used to score the same as
    # a correct one — that is how a `list` deck of nine bare `point` slides
    # scored 3/3 while missing every repo, the links index and the follow card.
    shape = 0
    if types and types[0] == "hook":
        shape += 1
    tail_order = ["links", "sources", "follow"]
    tail = [t for t in types if t in tail_order]
    if tail and tail == sorted(tail, key=tail_order.index):
        shape += 1
    if MIN_SLIDES <= len(slides) <= MAX_SLIDES:
        shape += 1

    # An enumeration has a shape of its own: the requested things must appear
    # as `repo` slides, not be flattened into prose bullets, and the reader
    # needs the links collected somewhere they can screenshot.
    enumeration_ok = None
    if intent == "list":
        enumeration_ok = bool(
            types.count("repo") >= 2
            and "links" in types
            and types[0] == "hook"
        )

    # Fact fidelity: do repo slides carry URLs that actually came from research?
    # Notes written before the repo-fields commit carry their URL only under
    # `sources`, so reading `url` alone reports every link on such an item as
    # fabricated when the real problem is that there was nothing to restore.
    known = set()
    for note in item.research or []:
        if note.get("url"):
            known.add(note["url"])
        raw = note.get("sources") or []
        if isinstance(raw, str):
            raw = re.findall(r"https?://[^\s'\"\]]+", raw)
        for url in raw:
            if url:
                known.add(str(url))
    repo_slides = [s for s in slides if s.get("type") == "repo"]
    bad_urls = sum(1 for s in repo_slides if s.get("url") and s["url"] not in known)

    # A deck of nothing but headline-and-bullets was the original complaint.
    rich = sum(1 for t in types if t not in ("hook", "point", "takeaway", "sources"))

    return {
        "id": item.id,
        "intent": intent,
        "slides": len(slides),
        "types": types,
        "distinct_types": len(set(types)),
        "rich_slides": rich,
        "empty_slides": empty,
        "overlong_headlines": overlong,
        "shape_score": shape,          # 0-3
        "enumeration_ok": enumeration_ok,   # None for non-list intents
        "bad_repo_urls": bad_urls,
        "hashtags": (doc.get("caption") or "").count("#"),
        "theme": doc.get("theme"),
    }


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"decks": 0}
    return {
        "decks": len(ok),
        "failed": len(rows) - len(ok),
        "avg_slides": round(statistics.mean(r["slides"] for r in ok), 1),
        "avg_distinct_types": round(statistics.mean(r["distinct_types"] for r in ok), 2),
        "avg_rich_slides": round(statistics.mean(r["rich_slides"] for r in ok), 2),
        "total_empty_slides": sum(r["empty_slides"] for r in ok),
        "total_overlong_headlines": sum(r["overlong_headlines"] for r in ok),
        "avg_shape_score": round(statistics.mean(r["shape_score"] for r in ok), 2),
        "total_bad_repo_urls": sum(r["bad_repo_urls"] for r in ok),
        "hashtags_over_limit": sum(1 for r in ok if r["hashtags"] > MAX_HASHTAGS),
        "themes_used": len({r["theme"] for r in ok if r["theme"]}),
        # Reported as a fraction so a single list item in the set cannot hide
        # behind fifteen news decks averaging it away.
        "enumerations_ok": "{}/{}".format(
            sum(1 for r in ok if r.get("enumeration_ok")),
            sum(1 for r in ok if r.get("enumeration_ok") is not None),
        ),
    }


def prompt_fingerprint(settings) -> str:
    """Identify the prompt a run scored.

    Runs were tagged but prompts were not, so a saved result could not be
    attributed to the prompt that produced it once the prompt moved on.
    """
    from pipeline.stages import compose as compose_mod

    mode = getattr(settings, "few_shot_examples", "off")
    parts = [compose_mod.SYSTEM]
    if mode != "off":
        from pipeline.stages import examples
        parts.append(json.dumps(examples.EXAMPLES, sort_keys=True))
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]
    return f"{digest}+fewshot:{mode}" if mode != "off" else digest


def load_golden() -> list:
    """Rebuild items from the frozen snapshot rather than the live database."""
    from pipeline.models import Item, Status

    data = json.loads(GOLDEN.read_text())
    items, modes = [], {}
    for case in data["cases"]:
        fields = dict(case["item"])
        item = Item(status=Status.SYNTHESIZED, **fields)
        modes[item.id] = case["failure_mode"]
        items.append(item)
    return items, modes, data["set_version"]


class Recording:
    """Wraps the client and keeps the last completion of each role.

    A failed case used to store only its error, so the three few-shot failures
    could not be diagnosed without paying for every call again — backwards,
    since a failure is exactly when the output matters.
    """

    def __init__(self, inner):
        self.inner = inner
        self.last: dict = {}

    def _wrap(self, role):
        async def call(*args, **kwargs):
            result = await getattr(self.inner, role)(*args, **kwargs)
            self.last[role] = result
            return result
        return call

    def __getattr__(self, name):
        if name in ("cheap", "good", "vision"):
            return self._wrap(name)
        return getattr(self.inner, name)


async def run(limit: int, tag: str | None, only: list[int] | None = None,
              golden: bool = False) -> None:
    settings = Settings.load()

    modes: dict[int, str] = {}
    set_version = None
    if golden:
        items, modes, set_version = load_golden()
        db = None
    else:
        db = await Database(settings.db_path).connect()
        items = await _items_from_db(db, limit, only)

    model = (settings.openrouter_model_good if settings.llm_provider == "openrouter"
             else settings.model_good)
    fingerprint = prompt_fingerprint(settings)
    print(f"provider={settings.llm_provider}  good-model={model[:52]}")
    print(f"prompt={fingerprint}"
          + (f"  golden set v{set_version}" if golden else "  (live db selection)"))
    print(f"scoring {len(items)} real items\n")

    rows = []
    async with httpx.AsyncClient() as http:
        llm = Recording(LLMClient(settings, http))
        for item in items:
            t0 = time.perf_counter()
            try:
                doc = await compose(item, llm, settings)
                row = score_deck(item, doc)
                row["seconds"] = round(time.perf_counter() - t0, 1)
                row["doc"] = doc
                row["failure_mode"] = modes.get(item.id)
                print(f"  item {item.id:3} {str(row['intent']):5} "
                      f"{row['slides']:2} slides  {row['distinct_types']} types  "
                      f"empty={row['empty_slides']}  long={row['overlong_headlines']}  "
                      f"shape={row['shape_score']}/3  {row['seconds']:5.1f}s"
                      f"  {modes.get(item.id, '')}")
            except Exception as exc:
                row = {"id": item.id, "error": f"{type(exc).__name__}: {exc}"[:120],
                       "failure_mode": modes.get(item.id),
                       # What the model actually returned, so the failure can
                       # be read off disk instead of re-run.
                       "raw": llm.last.get("good")}
                print(f"  item {item.id:3} FAILED {row['error']}")
            rows.append(row)

    if db is not None:
        await db.close()

    summary = summarise(rows)
    summary["prompt"] = fingerprint
    summary["set_version"] = set_version
    print("\n" + "=" * 58)
    for key, value in summary.items():
        print(f"  {key:26} {value}")

    if modes:
        print("\n  by failure mode:")
        for mode in sorted({m for m in modes.values()}):
            sub = [r for r in rows if r.get("failure_mode") == mode]
            ok = [r for r in sub if "error" not in r]
            if not ok:
                print(f"    {mode:28} 0/{len(sub)} composed")
                continue
            shape = statistics.mean(r["shape_score"] for r in ok)
            rich = statistics.mean(r["rich_slides"] for r in ok)
            print(f"    {mode:28} {len(ok)}/{len(sub)} composed  "
                  f"shape {shape:.2f}/3  rich {rich:.2f}")

    if tag:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f"{tag}.json").write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=2)
        )
        print(f"\nsaved -> data/eval/{tag}.json")


async def _items_from_db(db, limit: int, only: list[int] | None) -> list:
    items = []
    if only:
        # Targeting specific ids matters when one item is the whole question —
        # a single enumeration deck averaged into fifteen news decks says
        # nothing about the enumeration path.
        for item_id in only:
            item = await db.get_item(item_id)
            if item is not None:
                items.append(item)
    else:
        for row in await db.conn.execute_fetchall(
            "SELECT id FROM items WHERE brief IS NOT NULL AND length(brief) > 200"
            " ORDER BY id LIMIT ?", (limit,)
        ):
            items.append(await db.get_item(row["id"]))
    return items


def compare(a: str, b: str) -> None:
    left = json.loads((RESULTS / f"{a}.json").read_text())["summary"]
    right = json.loads((RESULTS / f"{b}.json").read_text())["summary"]
    print(f"{'metric':26} {a:>12} {b:>12}   change")
    print("-" * 66)
    for key in left:
        lv, rv = left[key], right.get(key)
        if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
            delta = rv - lv
            mark = "" if abs(delta) < 1e-9 else (f"  {delta:+g}")
            print(f"{key:26} {lv:>12} {rv:>12}{mark}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=16)
    ap.add_argument("--tag")
    ap.add_argument("--golden", action="store_true",
                    help="score the frozen golden set instead of live db rows")
    ap.add_argument("--items", help="comma-separated item ids to score instead of the first N")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()

    if args.compare:
        compare(*args.compare)
    else:
        only = [int(x) for x in args.items.split(",")] if args.items else None
        asyncio.run(run(args.limit, args.tag, only, args.golden))
