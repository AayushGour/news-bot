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

    # Substance metrics (pipeline.quality) recomputed from an already-saved
    # run's persisted decks (rows[].doc / rows[].raw) -- zero network calls,
    # zero model calls, safe with no API key configured at all.
    ./.venv/bin/python scripts/eval_compose.py --rescore golden_current
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
from pipeline.lease import run_host_script  # noqa: E402
from pipeline.integrity import (  # noqa: E402
    NON_CONTENT_TYPES,
    headline_count_slides,
    material_text,
    material_units,
    slide_backed,
)
from pipeline.llm import LLMClient  # noqa: E402
from pipeline.quality import (  # noqa: E402
    hook_headline,
    hook_similarity,
    score_substance,
    substance_composite,
)
from pipeline.stages.compose import (  # noqa: E402
    MAX_HASHTAGS,
    MAX_SLIDES,
    MIN_SLIDES,
    REQUIRED_BODY_FIELDS,
    compose,
)

RESULTS = ROOT / "data" / "eval"
GOLDEN = RESULTS / "golden.json"

#: Per-type headline limits as stated in the prompt. Measuring how often the
#: model exceeds what it was told is the point.
HEADLINE_LIMITS = {
    "hook": 60, "point": 45, "facts": 45, "code": 45, "notes": 45, "flow": 45,
    "compare": 45, "quote": 40, "photo": 45, "kpi": 45, "chart": 45,
    "takeaway": 55, "sources": 45, "follow": 40, "links": 45, "repo": 60,
}


def _slide_is_empty(slide: dict) -> bool:
    """No content for this slide's own type contract (REQUIRED_BODY_FIELDS).

    T100: this used to check a flat, type-agnostic `BODY_FIELDS` tuple
    against every slide regardless of type -- a third hand-maintained copy
    of the body contract (T78 made `REQUIRED_BODY_FIELDS` the single,
    per-type source of truth the schema builder and `normalise_slides` both
    consume). The consequence was not cosmetic: a `links` slide carrying
    only `sub` (no `links`) has "sub" in the flat tuple, so it scored as
    non-empty here while `normalise_slides` drops that exact slide in
    production for carrying none of `links`'s own required field -- the
    harness rated a broken deck `0 empty_slides`. Reading the per-type
    registry instead of a re-typed copy is what makes this check agree with
    production. An unrecognised type has no declared body contract to
    satisfy, so it scores empty too -- normalise_slides would drop it.
    """
    return not any(slide.get(f) for f in REQUIRED_BODY_FIELDS.get(slide.get("type"), ()))


def score_deck(item, doc: dict) -> dict:
    """Mechanical quality signals for one composed deck."""
    slides = doc.get("slides") or []
    types = [s.get("type") for s in slides]
    intent = item.intent or "news"

    empty = sum(1 for s in slides if _slide_is_empty(s))
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

    # T104: "the heading says 7 best but the rest of the image contains only
    # max of 4 points" -- a headline promising a count no metric here ever
    # compared against what the deck actually delivered. See
    # pipeline.integrity.headline_count_slides for the (deliberately narrow)
    # scope rule.
    count_mismatches = headline_count_slides(slides, intent)

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
        "headline_count_mismatch": len(count_mismatches),
        "hashtags": (doc.get("caption") or "").count("#"),
        "theme": doc.get("theme"),
        **material_fields(item, doc),      # reported-only (T249)
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
        "total_headline_count_mismatch": sum(r["headline_count_mismatch"] for r in ok),
        "hashtags_over_limit": sum(1 for r in ok if r["hashtags"] > MAX_HASHTAGS),
        "themes_used": len({r["theme"] for r in ok if r["theme"]}),
        "avg_content_slides": round(statistics.mean(r["content_slides"] for r in ok), 2),
        "avg_material_units": round(statistics.mean(r["material_units"] for r in ok), 2),
        "total_unsupported_rich": sum(r["unsupported_rich"] for r in ok),
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
    # T85/D16: this is the path that actually calls a model (--rescore and
    # --compare never reach here — see their own docstrings). An
    # openrouter/free or paid slug here would make this exact eval arm
    # unreproducible, silently, which is the one thing T69 exists to prevent.
    settings.validate_model_config()

    modes: dict[int, str] = {}
    set_version = None
    if golden:
        items, modes, set_version = load_golden()
        db = None
    else:
        db = await Database(settings.db_path).connect()
        items = await _items_from_db(db, limit, only)

    good_provider = settings.provider_for("good")
    model = (settings.openrouter_model_good if good_provider == "openrouter"
             else settings.model_good)
    fingerprint = prompt_fingerprint(settings)
    print(f"provider={good_provider}  good-model={model[:52]}")
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
                      f"shape={row['shape_score']}/3  "
                      f"content={row['content_slides']} units={row['material_units']} "
                      f"unsupported={row['unsupported_rich']}  {row['seconds']:5.1f}s"
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


def material_fields(item, deck: dict) -> dict:
    """Reported-only deck-length fields (T249; D217/D219/D230). Not in COMPOSITE_WEIGHTS.

    House rule 11: reads slides (model-written) and the item's research + raw_text.
    """
    slides = [s for s in (deck.get("slides") or []) if isinstance(s, dict)]
    content = [s for s in slides if s.get("type") not in NON_CONTENT_TYPES]
    material = material_text(item)
    return {
        "content_slides": len(content),
        "material_units": material_units(item),
        "unsupported_rich": sum(1 for s in content if not slide_backed(s, material)),
    }


def _deck_from_row(row: dict) -> tuple[dict | None, str | None]:
    """The deck a saved row actually produced, and which field it came from.

    A successful row persists the finished deck at ``doc`` (T23/T38). A
    failed row has no ``doc`` — compose raised before it could build one —
    but does carry the model's last raw completion at ``raw``, which is the
    same {slides, caption, hashtags, theme} shape before post-processing.
    Neither is present when even the raw completion never came back (e.g. a
    provider 404 before any content arrived); that row cannot be scored and
    this returns (None, None) rather than crashing on it.

    The source is returned alongside the deck (not just the deck) because
    ``doc`` is a validated, post-processed deck and ``raw`` is the model's
    unvalidated last completion — mixing the two into one "decks scored"
    average answers a composition question ("how many rows had a doc?"), not
    a quality question, unless the caller can also see the split.
    """
    doc = row.get("doc")
    if isinstance(doc, dict) and doc.get("slides"):
        return doc, "doc"
    raw = row.get("raw")
    if isinstance(raw, dict) and raw.get("slides"):
        return raw, "raw"
    return None, None


def rescore(tag: str) -> None:
    """Recompute substance metrics for an already-saved run.

    Zero network calls, zero model calls: the deck is already on disk at
    rows[].doc (or rows[].raw for a failed row — see `_deck_from_row`), and
    the item text `grounding_ratio`/`code_preserved` need comes from the
    golden set those rows were scored against (`load_golden`, a local JSON
    read). This is why --rescore works with no API key configured at all —
    there is nothing here that could make a call even if one were allowed to.

    Refuses to run against a run whose ``summary.set_version`` doesn't
    identify (or doesn't match) the golden set on disk. A live-db run's row
    ids are database ids, not golden-set ids, and golden-set ids overlap
    database ids heavily (see `load_golden`'s ids vs `data/eval/baseline.json`
    in the T68 review) — pairing them silently scores a row against a
    different item's brief and prints a plausible-looking number for it. An
    off-by-one pairing measured grounding at 0.0009 against a correct 0.0992
    on the same data; both read as "grounding is bad" and nothing in the old
    output told the two apart.
    """
    path = RESULTS / f"{tag}.json"
    if not path.exists():
        available = sorted(p.stem for p in RESULTS.glob("*.json"))
        raise SystemExit(
            f"data/eval/{tag}.json does not exist. Available tags: "
            + (", ".join(available) if available else "(none)")
        )
    saved = json.loads(path.read_text())
    if "rows" not in saved:
        raise SystemExit(
            f"data/eval/{tag}.json has no \"rows\" key — not a saved eval_compose.py run"
        )
    rows = saved["rows"]

    run_version = saved.get("summary", {}).get("set_version")
    if run_version is None:
        raise SystemExit(
            f"data/eval/{tag}.json was scored against a live db selection "
            "(summary.set_version is None) -- its row ids are database ids, not "
            "golden-set ids, so pairing them against the golden set would silently "
            "score each row against a different item's brief. Re-run with --golden "
            "(and --tag) to get a versioned run, then --rescore that tag instead."
        )
    items, _modes, golden_version = load_golden()
    if run_version != golden_version:
        raise SystemExit(
            f"data/eval/{tag}.json was scored against golden set v{run_version}, but "
            f"data/eval/golden.json on disk is v{golden_version} -- refusing to pair a "
            "run against a golden set version it was not scored against. Re-run "
            "--golden against the current golden set."
        )
    by_id = {item.id: item for item in items}

    scored, unscored = [], 0
    for row in rows:
        item = by_id.get(row["id"])
        deck, source = _deck_from_row(row)
        if item is None or deck is None:
            unscored += 1
            continue
        metrics = score_substance(item, deck)
        metrics["id"] = row["id"]
        metrics["hook"] = hook_headline(deck)
        metrics["source"] = source

        metrics.update(material_fields(item, deck))
        scored.append(metrics)

    # hook_similarity is a run-level property (see pipeline.quality) — the
    # same value applies to every deck's composite in this run, doc and raw
    # rows alike (a repeated opener is a run-wide reader-visible failure
    # regardless of which field the deck happened to be persisted under).
    run_hook_similarity = hook_similarity([m["hook"] for m in scored if m["hook"]])
    for metrics in scored:
        metrics["substance_composite"] = substance_composite(
            metrics, hook_similarity=run_hook_similarity
        )

    print(f"rescoring data/eval/{tag}.json  (golden set v{run_version}, "
          f"zero network calls)")
    print(f"{len(scored)}/{len(rows)} rows scored"
          + (f"  ({unscored} had no item match or no deck to score)" if unscored else "")
          + "\n")
    for m in scored:
        print(f"  item {m['id']:3}  src={m['source']:3}  composite={m['substance_composite']:5.1f}  "
              f"numeric={m['numeric_density']:5.2f}  proper_noun={m['proper_noun_density']:5.2f}  "
              f"grounding={m['grounding_ratio']:.3f}  filler={m['filler_count']}  "
              f"bare_bullets={m['bare_bullet_ratio']:.2f}  "
              f"code_preserved={m['code_preserved']}  "
              f"content_slides={m['content_slides']}  material_units={m['material_units']}  "
              f"unsupported_rich={m['unsupported_rich']}")

    def _group_summary(group: list[dict]) -> dict:
        def _avg(key: str) -> float | None:
            values = [m[key] for m in group if isinstance(m.get(key), (int, float))]
            return round(statistics.mean(values), 3) if values else None

        applicable_code = [m["code_preserved"] for m in group if m["code_preserved"] is not None]
        code_rate = (f"{sum(applicable_code)}/{len(applicable_code)}" if applicable_code
                     else "0/0 (no fenced block in any source brief)")
        return {
            "decks_scored": len(group),
            "avg_substance_composite": _avg("substance_composite"),
            "avg_numeric_density": _avg("numeric_density"),
            "avg_proper_noun_density": _avg("proper_noun_density"),
            "avg_grounding_ratio": _avg("grounding_ratio"),
            "avg_filler_count": _avg("filler_count"),
            "avg_bare_bullet_ratio": _avg("bare_bullet_ratio"),
            "code_preserved_rate": code_rate,
            "avg_content_slides": _avg("content_slides"),
            "avg_material_units": _avg("material_units"),
            "total_unsupported_rich": sum(m["unsupported_rich"] for m in group),
        }

    # `doc` decks are validated/post-processed; `raw` decks are a failed
    # row's last unvalidated completion. Mixing them into one average blurs
    # a composition question (how many of each landed in this run) into what
    # looks like a quality question — see the T68 review's golden_current
    # (19 doc / 5 raw) vs golden_listonly (24 doc / 0 raw) example, where
    # about a third of the apparent gap between the two runs was that split,
    # not substance. Report all three views so a reader can tell them apart.
    doc_rows = [m for m in scored if m["source"] == "doc"]
    raw_rows = [m for m in scored if m["source"] == "raw"]

    print("\n" + "=" * 58)
    print("  overall (doc + raw)")
    overall = _group_summary(scored)
    overall["decks_unscored"] = unscored
    overall["hook_similarity"] = run_hook_similarity
    for key, value in overall.items():
        print(f"    {key:26} {value}")

    print("\n  doc only (validated decks)")
    for key, value in _group_summary(doc_rows).items():
        print(f"    {key:26} {value}")

    print("\n  raw only (unvalidated last completion)")
    if raw_rows:
        for key, value in _group_summary(raw_rows).items():
            print(f"    {key:26} {value}")
    else:
        print("    (none in this run)")


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
    ap.add_argument("--rescore", metavar="TAG",
                    help="substance-score an already-saved run's persisted decks "
                         "(data/eval/TAG.json) -- zero network calls")
    args = ap.parse_args()

    if args.rescore:
        rescore(args.rescore)
    elif args.compare:
        compare(*args.compare)
    else:
        only = [int(x) for x in args.items.split(",")] if args.items else None
        code = run_host_script(
            "eval_compose", lambda: asyncio.run(run(args.limit, args.tag, only, args.golden)))
        if code:
            raise SystemExit(code)
