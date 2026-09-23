"""T120 -- measure `grounding_ratio`'s provenance sensitivity. MEASURE ONLY.

`grounding_ratio` is the programme's anchor metric and its four inputs are
FROZEN (house rule 12 / D76): `deck_body_text`/`_slide_text`'s field set,
`_fold_apostrophes`' normalisation, `_ngrams`' `n`, and
`source_text_from_item`'s field set. This script changes none of them -- it
imports `_slides`, `_slide_text`, `_ngrams`, `source_text_from_item` and
`STRUCTURAL_SLIDE_TYPES` straight from `pipeline.quality` (AC8) and only adds
new orchestration around them, so every number below is a fact about the
SHIPPED metric, not about a parallel copy of it that could quietly drift.

Why this needs its own task rather than a one-line "exclude structural
slides and see": `deck_body_text` (and therefore `grounding_ratio`, which
reads `_slide_text` directly) is not provenance-aware -- it reads text the
PIPELINE wrote (`ensure_closing_slide`, `ensure_links_slide`,
`_restore_repo_facts`) as if the MODEL wrote it. D70 already measured that
CTA text (`links`/`sources`/`follow`) deflates `filler_count`-style metrics
by being penalised for a repair the code performed. D75 is the mirror image
for `grounding_ratio` specifically, and the mirror inverts the sign:
`_restore_repo_facts` (`compose.py:1052-1053`) copies `note["detail"]`
verbatim into a `repo` slide's `sub` when the model left it blank, `sub` is
one of `_slide_text`'s `_SCALAR_TEXT_FIELDS`, and `source_text_from_item` IS
brief + every note's claim + detail -- so that copied text is scored as
grounded BY CONSTRUCTION. CTA text deflates grounding; repo `sub` inflates
it. Net direction on any given deck is not obvious from the code alone,
which is exactly why this is a measurement task, not a reasoning-from-the-
code-walk task (see D65: a mechanism confirmed by reading the code is not
the same fact as its measured frequency).

Three predictions were pre-registered by the architect BEFORE this script
was run (D75). This script reports whether each held, in those words, not
whether a "corrected" version of them holds.

    ./.venv/bin/python scripts/measure_grounding_provenance.py   # if a venv exists
    python3 scripts/measure_grounding_provenance.py              # stdlib only otherwise

Zero dependencies beyond the stdlib and `pipeline.models`/`pipeline.quality`
(both stdlib-only per house rule 6), so this runs with no `uv sync` and no
API key configured, exactly like `eval_compose.py --rescore` (same
justification: the decks are already on disk, nothing here makes a network
or model call).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline.models import Item, Status  # noqa: E402
from pipeline.quality import (  # noqa: E402
    STRUCTURAL_SLIDE_TYPES,
    _ngrams,
    _slide_text,
    _slides,
    grounding_ratio,
    source_text_from_item,
)

EVAL_DIR = ROOT / "data" / "eval"
GOLDEN_CASES_FILE = EVAL_DIR / "golden.json"
GOLDEN_SETS: tuple[str, ...] = ("golden_current", "golden_listonly")

#: D73's stated partition of `STRUCTURAL_SLIDE_TYPES` (`compose.py`'s
#: `ensure_closing_slide` / `ensure_links_slide` write only these three
#: types' prose): {links, sources, follow}. NOT yet exported by
#: `pipeline.quality` -- T74 is adding this split in parallel (D73/D75) and
#: this constant should be imported from there once it lands, per house rule
#: 1 (one registry, many readers). Re-typed here only because the source of
#: truth does not exist yet; the assertion right below pins it to
#: `quality.STRUCTURAL_SLIDE_TYPES` so this script breaks loudly, not
#: silently, the moment that set changes shape without this constant moving
#: with it.
PIPELINE_AUTHORED_TYPES: frozenset[str] = frozenset({"links", "sources", "follow"})
#: D75's "hook+takeaway" class -- the two mandatory model-written bookends
#: that are not "repo" (singled out separately, since that is the one type
#: `_restore_repo_facts` can rewrite).
BOOKEND_TYPES: frozenset[str] = frozenset({"hook", "takeaway"})
REPO_TYPE = "repo"

if PIPELINE_AUTHORED_TYPES | BOOKEND_TYPES | {REPO_TYPE} != STRUCTURAL_SLIDE_TYPES:
    raise AssertionError(
        "D73's partition (PIPELINE_AUTHORED_TYPES + BOOKEND_TYPES + {repo}) no "
        "longer reconstructs pipeline.quality.STRUCTURAL_SLIDE_TYPES -- that set "
        f"is now {sorted(STRUCTURAL_SLIDE_TYPES)!r}. Update this script's "
        "partition before trusting any table below."
    )

#: The four mutually-exclusive, exhaustive slide classes this script reports
#: by (AC3). Every slide type is in exactly one of these, or in none (the
#: fourth, "content", is everything NOT in STRUCTURAL_SLIDE_TYPES -- the same
#: complement `bare_bullet_ratio` uses).
CLASS_ORDER: tuple[str, ...] = ("pipeline_authored", "repo", "hook_takeaway", "content")


def _classify(slide_type: str | None) -> str:
    if slide_type in PIPELINE_AUTHORED_TYPES:
        return "pipeline_authored"
    if slide_type == REPO_TYPE:
        return "repo"
    if slide_type in BOOKEND_TYPES:
        return "hook_takeaway"
    return "content"


# --------------------------------------------------------------- loading


def load_golden_items() -> tuple[dict[int, Item], int]:
    """Rebuild `Item`s from the frozen golden-set snapshot, keyed by id.

    Same reconstruction `eval_compose.py:load_golden` does (`Item(status=...,
    **fields)` off `case["item"]`) -- not re-derived independently, because a
    second, slightly different loader is exactly the "second copy of a
    contract" house rule 3 forbids. Not imported from `eval_compose.py`
    itself because that module imports `httpx`/`dotenv`/`LLMClient` at
    top level for its live-call path, which this measure-only script has no
    business depending on.
    """
    data = json.loads(GOLDEN_CASES_FILE.read_text())
    items: dict[int, Item] = {}
    for case in data["cases"]:
        fields = dict(case["item"])
        item = Item(status=Status.SYNTHESIZED, **fields)
        items[item.id] = item
    return items, data["set_version"]


def load_doc_rows(set_name: str) -> tuple[list[dict], int]:
    """This golden set's rows that carry a validated `doc` (D17: doc-only).

    A `raw` row is the model's last unvalidated completion before compose
    raised -- 5 of `golden_current`'s 24 rows are `raw`-only (D22) and are
    excluded here by construction, not filtered after the fact, so nothing
    downstream can accidentally re-include one.
    """
    data = json.loads((EVAL_DIR / f"{set_name}.json").read_text())
    rows = [
        row
        for row in data["rows"]
        if isinstance(row.get("doc"), dict) and row["doc"].get("slides")
    ]
    return rows, data["summary"].get("set_version")


# --------------------------------------------------------- class breakdown


def class_grams_for_deck(
    doc: dict, source_grams: set[tuple[str, ...]]
) -> dict[str, tuple[int, int, int]]:
    """(fragments, distinct 3-grams, grounded 3-grams) per slide class, for
    one deck. `distinct` and `grounded` are each deduped WITHIN the class for
    this deck (a set, exactly like `grounding_ratio` dedupes within a whole
    deck) -- not deduped across decks, and not deduped across classes, so a
    3-gram that happens to appear in both a `repo` slide and a `content`
    slide of the SAME deck is counted once in each class's total. That is a
    deliberate choice for a per-class breakdown (each class's number answers
    "how much does this class contribute on its own"), not a partition of one
    shared total -- summing the four class totals can therefore exceed the
    deck's own single `grounding_ratio` numerator/denominator very slightly
    on the rare deck where phrasing repeats across classes.
    """
    per_class: dict[str, set[tuple[str, ...]]] = {c: set() for c in CLASS_ORDER}
    fragments: dict[str, int] = {c: 0 for c in CLASS_ORDER}
    for slide in _slides(doc):
        cls = _classify(slide.get("type"))
        for fragment in _slide_text(slide):
            fragments[cls] += 1
            per_class[cls] |= _ngrams(str(fragment))
    return {
        c: (fragments[c], len(per_class[c]), len(per_class[c] & source_grams))
        for c in CLASS_ORDER
    }


def aggregate_class_breakdown(
    rows: list[dict], items: dict[int, Item]
) -> dict[str, dict[str, int]]:
    """Sum `class_grams_for_deck` over every doc row in `rows`."""
    totals = {
        c: {"fragments": 0, "distinct_grams": 0, "grounded_grams": 0}
        for c in CLASS_ORDER
    }
    for row in rows:
        item = items.get(row["id"])
        if item is None:
            continue
        source_grams = _ngrams(source_text_from_item(item))
        per_deck = class_grams_for_deck(row["doc"], source_grams)
        for cls, (frags, distinct, grounded) in per_deck.items():
            totals[cls]["fragments"] += frags
            totals[cls]["distinct_grams"] += distinct
            totals[cls]["grounded_grams"] += grounded
    return totals


# ------------------------------------------------------- repo sub fallback


def repo_sub_fallback_frequency(
    rows: list[dict], items: dict[int, Item]
) -> dict[str, int]:
    """How often a `repo` slide's `sub` is consistent with having come from
    `_restore_repo_facts`' fallback (`compose.py:1052-1053`:
    ``slide["sub"] = str(note["detail"]).strip()`` when the model left `sub`
    blank).

    This is a PROXY, not a direct observation -- the saved doc is the
    post-fallback artifact, so nothing on disk distinguishes "the pipeline
    wrote this via the fallback" from "the model independently wrote the same
    sentence the research note already contains" (which is itself a
    plausible way for a model to plagiarise its own source material). Both
    `exact_match` (sub == some note's stripped detail, the literal condition
    the fallback assignment produces) and `substring_match` (sub is a
    substring of some note's detail, the AC's literal wording, and a
    superset of exact_match) are reported so a reader can see the proxy's
    range rather than one cherry-picked number. This is exactly D65's trap:
    the MECHANISM is certain from the code-walk; the FREQUENCY is not, and
    this is the closest this script can get to it without instrumenting
    `_restore_repo_facts` itself (out of scope -- MEASURE ONLY, T120).
    """
    repo_slides = with_sub = exact = substring = eligible = 0
    for row in rows:
        item = items.get(row["id"])
        if item is None:
            continue
        notes = item.research or []
        by_name = {str(n.get("name", "")).lower(): n for n in notes if n.get("name")}
        details = [str(n.get("detail") or "").strip() for n in notes]
        details = [d for d in details if d]
        for slide in _slides(row["doc"]):
            if slide.get("type") != REPO_TYPE:
                continue
            repo_slides += 1
            # "eligible" = the fallback's own trigger condition, checkable
            # from the outside: a name-matched note exists AND it carries a
            # non-empty detail. If this is true and `sub` is STILL empty,
            # either the fallback did not run on this artifact, or something
            # downstream erased its result -- see the printed note below.
            note = by_name.get(str(slide.get("name", "")).lower())
            if note is not None and note.get("detail"):
                eligible += 1
            sub = str(slide.get("sub") or "").strip()
            if not sub:
                continue
            with_sub += 1
            if sub in details:
                exact += 1
                substring += 1
            elif any(sub in detail for detail in details):
                substring += 1
    return {
        "repo_slides": repo_slides,
        "eligible_for_fallback": eligible,
        "repo_slides_with_sub": with_sub,
        "exact_match": exact,
        "substring_match": substring,
    }


# --------------------------------------------------------- scoped ratios


def scoped_grounding_ratio(
    doc: dict, source_text: str, excluded_types: frozenset[str]
) -> float:
    """`grounding_ratio`'s own per-fragment-union composition (D76's
    docstring), parameterised by which slide TYPES are dropped from the deck
    side before building `deck_grams`.

    Deliberately mirrors `quality.grounding_ratio` line for line rather than
    calling it, because `grounding_ratio` itself has no slide-filtering hook
    -- that hook is the new thing this measurement task needs, and house
    rule 12 forbids adding it to the frozen function itself outside of a
    dedicated re-baselining task. The source side is untouched in every
    scope: `source_text_from_item`'s field set is one of the four frozen
    inputs (D76) and this script does not touch it. Every primitive used
    (`_slides`, `_slide_text`, `_ngrams`) is imported from `pipeline.quality`
    (AC8), so tokenisation and the apostrophe fold stay byte-identical to the
    shipped metric -- verified below by `_self_check_matches_shipped_metric`.
    """
    deck_grams: set[tuple[str, ...]] = set()
    for slide in _slides(doc):
        if slide.get("type") in excluded_types:
            continue
        for fragment in _slide_text(slide):
            deck_grams |= _ngrams(str(fragment))
    if not deck_grams:
        return 0.0
    source_grams = _ngrams(source_text or "")
    if not source_grams:
        return 0.0
    return round(len(deck_grams & source_grams) / len(deck_grams), 4)


def _self_check_matches_shipped_metric(
    rows: list[dict], items: dict[int, Item]
) -> None:
    """Two properties that must both hold, on every doc row in both golden
    sets, or every number this script prints is measuring the wrong thing:

    1. `scoped_grounding_ratio(doc, src, frozenset())` (no exclusion) must
       equal `quality.grounding_ratio(doc, src)` exactly -- proof this script
       measures the SHIPPED metric, not a copy that has quietly drifted from
       it.
    2. `scoped_grounding_ratio(doc, src, <every type actually in this
       deck>)` must be 0.0 -- excluding every slide the deck has must leave
       zero deck-side grams, hence a defined 0.0 (empty-denominator branch),
       never a value indistinguishable from "no exclusion happened". This is
       the one that actually exercises the filter: a no-op bug in the
       ``if slide.get("type") in excluded_types`` line (e.g. an accidentally
       constant condition) passes property 1 unchanged -- frozenset() has no
       members to filter either way -- but fails property 2 outright, since
       a broken filter leaves the full deck's grams in place instead of
       emptying them. Property 2 is what a mutation pass (house rule:
       "mutate, then rerun; still green means the check asserts nothing")
       actually needs here; property 1 alone is decorative for this failure
       mode.

    Raises loudly rather than printing a maybe-someone-notices warning.
    """
    for row in rows:
        item = items.get(row["id"])
        if item is None:
            continue
        source_text = source_text_from_item(item)

        shipped = grounding_ratio(row["doc"], source_text)
        scoped_none_excluded = scoped_grounding_ratio(
            row["doc"], source_text, frozenset()
        )
        if shipped != scoped_none_excluded:
            raise AssertionError(
                f"self-check failed on row id={row['id']}: shipped "
                f"grounding_ratio={shipped} but scoped(as-shipped)="
                f"{scoped_none_excluded} -- scoped_grounding_ratio has "
                "drifted from quality.grounding_ratio"
            )

        types_present = frozenset(s.get("type") for s in _slides(row["doc"]))
        scoped_all_excluded = scoped_grounding_ratio(
            row["doc"], source_text, types_present
        )
        if scoped_all_excluded != 0.0:
            raise AssertionError(
                f"self-check failed on row id={row['id']}: excluding every "
                f"type this deck actually has ({sorted(types_present)}) "
                f"scored {scoped_all_excluded}, not 0.0 -- the exclusion "
                "filter in scoped_grounding_ratio is not actually filtering"
            )


SCOPES: dict[str, frozenset[str]] = {
    "as-shipped": frozenset(),
    "minus PIPELINE_AUTHORED_TYPES {links, sources, follow}": PIPELINE_AUTHORED_TYPES,
    "minus STRUCTURAL_SLIDE_TYPES {hook, takeaway, sources, links, follow, repo}": STRUCTURAL_SLIDE_TYPES,
}
#: Not one of the three AC5 scopes -- added only to test prediction 3, which
#: is specifically about excluding `repo` slides (not the other five
#: structural types) from both sides of the golden_current/golden_listonly
#: comparison.
PREDICTION_3_SCOPE: dict[str, frozenset[str]] = {
    "minus repo only (prediction 3 check)": frozenset({REPO_TYPE}),
}


def scored_ratios(
    rows: list[dict], items: dict[int, Item], excluded_types: frozenset[str]
) -> list[float]:
    out = []
    for row in rows:
        item = items.get(row["id"])
        if item is None:
            continue
        out.append(
            scoped_grounding_ratio(
                row["doc"], source_text_from_item(item), excluded_types
            )
        )
    return out


# --------------------------------------------------------------- printing


def print_config_header(
    doc_rows_by_set: dict[str, list[dict]], fragment_counts: dict[str, int]
) -> None:
    print("=" * 72)
    print("CONFIGURATION (house rule 4 -- a measurement without its")
    print("configuration is not reproducible)")
    print("=" * 72)
    print(f"  date                    {datetime.now(tz=UTC).date().isoformat()}")
    print("  script                  scripts/measure_grounding_provenance.py")
    print("  files read              data/eval/golden.json (item briefs + research)")
    for name in GOLDEN_SETS:
        print(f"                          data/eval/{name}.json")
    print("  row filter              doc rows only (D17) -- a row counts only if")
    print("                          row['doc'] is a dict with a non-empty 'slides'")
    print("                          list; 'raw' (unvalidated) rows are excluded")
    print("                          by construction, not filtered after scoring")
    print(
        f"  slide classes (AC3)     pipeline_authored = {sorted(PIPELINE_AUTHORED_TYPES)}"
    )
    print(f"                          repo               = ['{REPO_TYPE}']")
    print(f"                          hook_takeaway      = {sorted(BOOKEND_TYPES)}")
    print(
        "                          content             = everything else "
        "(complement of STRUCTURAL_SLIDE_TYPES)"
    )
    print("  scopes (AC5)            as-shipped: no exclusion")
    print(
        f"                          minus PIPELINE_AUTHORED_TYPES: "
        f"{sorted(PIPELINE_AUTHORED_TYPES)}"
    )
    print(
        f"                          minus STRUCTURAL_SLIDE_TYPES: "
        f"{sorted(STRUCTURAL_SLIDE_TYPES)}"
    )
    print(f"                          [prediction-3 only] minus repo: ['{REPO_TYPE}']")
    print(
        "  frozen inputs untouched _slide_text field set, _fold_apostrophes, "
        "_ngrams(n=3),"
    )
    print("                          source_text_from_item field set -- all imported")
    print("                          from pipeline.quality unmodified (D76, AC8)")
    for name in GOLDEN_SETS:
        rows = doc_rows_by_set[name]
        print(
            f"  {name:22} {len(rows)} doc decks, "
            f"{fragment_counts[name]} prose fragments"
        )
    print()


def print_class_breakdown(name: str, totals: dict[str, dict[str, int]]) -> None:
    print(f"--- {name}: distinct / grounded 3-grams by slide class (AC3) ---")
    header = (
        f"  {'class':18} {'fragments':>10} {'distinct 3g':>12} "
        f"{'grounded 3g':>12} {'grounded %':>11}"
    )
    print(header)
    for cls in CLASS_ORDER:
        t = totals[cls]
        pct = (
            t["grounded_grams"] / t["distinct_grams"] * 100
            if t["distinct_grams"]
            else 0.0
        )
        print(
            f"  {cls:18} {t['fragments']:>10} {t['distinct_grams']:>12} "
            f"{t['grounded_grams']:>12} {pct:>10.1f}%"
        )
    print()


def print_fallback_frequency(name: str, stats: dict[str, int]) -> None:
    with_sub = stats["repo_slides_with_sub"]
    exact_pct = (stats["exact_match"] / with_sub * 100) if with_sub else 0.0
    sub_pct = (stats["substring_match"] / with_sub * 100) if with_sub else 0.0
    eligible_pct = (
        stats["eligible_for_fallback"] / stats["repo_slides"] * 100
        if stats["repo_slides"]
        else 0.0
    )
    print(f"--- {name}: repo `sub` vs `_restore_repo_facts` fallback proxy (AC4) ---")
    print(f"  repo slides (total)                {stats['repo_slides']}")
    print(
        f"  eligible for the fallback           {stats['eligible_for_fallback']} "
        f"({eligible_pct:.1f}%) -- name-matched note exists AND carries a "
        f"non-empty detail"
    )
    print(f"  repo slides with a non-empty sub   {with_sub}")
    print(
        f"  sub == some note.detail (exact)    {stats['exact_match']} "
        f"({exact_pct:.1f}% of repo subs)"
    )
    print(
        f"  sub in some note.detail (substr)   {stats['substring_match']} "
        f"({sub_pct:.1f}% of repo subs)"
    )
    if stats["eligible_for_fallback"] > 0 and with_sub == 0:
        print(
            f"  ANOMALY: {stats['eligible_for_fallback']} repo slide(s) satisfy the "
            f"fallback's own trigger condition (compose.py:1052-1053) yet show NO "
            f"`sub` at all in this saved artifact. Either the fallback did not run "
            f"when this golden set was generated, or it ran and something "
            f"downstream erased its result -- current compose.py shows nothing "
            f"between _restore_repo_facts (line ~629) and the saved doc that "
            f"would do that. Read literally: this data/eval/*.json snapshot does "
            f"not reflect current compose.py's repo-sub behaviour, so the 0% "
            f"below is a fact about THIS ARTIFACT, not a proven fact about "
            f"current production frequency -- exactly D65's trap, discovered "
            f"rather than assumed. A fresh --golden re-run is required before "
            f"this number can be trusted as current."
        )
    print()


def compute_all_means(
    doc_rows_by_set: dict[str, list[dict]], items: dict[int, Item]
) -> dict[str, dict[str, float]]:
    """`means[set_name][scope_label]` -- mean per-deck `grounding_ratio` under
    every scope (the three AC5 scopes plus the prediction-3-only "minus repo"
    scope), for both golden sets. Computed once and reused by every print
    function and by `evaluate_predictions`, rather than recomputed per call
    (DRY, coding-standards.md non-negotiable) -- with 24+19 decks x 4 scopes
    this is cheap, but a fifth caller re-deriving it by hand is exactly the
    "second copy of a contract" house rule 3 forbids for a number this task
    hangs its verdicts on.
    """
    means: dict[str, dict[str, float]] = {}
    for name in GOLDEN_SETS:
        rows = doc_rows_by_set[name]
        means[name] = {}
        for label, excluded in {**SCOPES, **PREDICTION_3_SCOPE}.items():
            values = scored_ratios(rows, items, excluded)
            means[name][label] = round(statistics.mean(values), 4) if values else 0.0
    return means


def print_scoped_ratios(name: str, means: dict[str, float]) -> None:
    print(f"--- {name}: grounding_ratio under three scopes (AC5) ---")
    baseline = means["as-shipped"]
    for label in (*SCOPES, *PREDICTION_3_SCOPE):
        mean = means[label]
        if label == "as-shipped":
            print(f"  {label:62} {mean:.4f}  (reference)")
            continue
        rel = ((mean - baseline) / baseline * 100) if baseline else float("nan")
        print(
            f"  {label:62} {mean:.4f}  delta {mean - baseline:+.4f}  "
            f"({rel:+.1f}% relative)"
        )
    print()


# --------------------------------------------------------------- main


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()

    items, set_version = load_golden_items()
    doc_rows_by_set: dict[str, list[dict]] = {}
    fragment_counts: dict[str, int] = {}
    for name in GOLDEN_SETS:
        rows, run_version = load_doc_rows(name)
        if run_version != set_version:
            raise SystemExit(
                f"{name}.json was scored against golden set v{run_version}, but "
                f"data/eval/golden.json on disk is v{set_version} -- refusing to "
                "pair rows against briefs they were not scored against (same "
                "guard as eval_compose.py --rescore)."
            )
        doc_rows_by_set[name] = rows
        fragment_counts[name] = sum(
            sum(len(_slide_text(s)) for s in _slides(row["doc"])) for row in rows
        )

    all_rows = [r for rows in doc_rows_by_set.values() for r in rows]
    _self_check_matches_shipped_metric(all_rows, items)

    print_config_header(doc_rows_by_set, fragment_counts)

    for name in GOLDEN_SETS:
        rows = doc_rows_by_set[name]
        totals = aggregate_class_breakdown(rows, items)
        print_class_breakdown(name, totals)

    for name in GOLDEN_SETS:
        rows = doc_rows_by_set[name]
        stats = repo_sub_fallback_frequency(rows, items)
        print_fallback_frequency(name, stats)

    means = compute_all_means(doc_rows_by_set, items)
    for name in GOLDEN_SETS:
        print_scoped_ratios(name, means[name])

    print("=" * 72)
    print("gap check (prediction 3): golden_listonly - golden_current, per scope")
    print("=" * 72)
    for label in (*SCOPES, *PREDICTION_3_SCOPE):
        gap = means["golden_listonly"][label] - means["golden_current"][label]
        print(f"  {label:62} gap {gap:+.4f}")
    print()

    print_predictions_verdict(means)


# --------------------------------------------------------- predictions (AC6)


_PIPELINE_LABEL = "minus PIPELINE_AUTHORED_TYPES {links, sources, follow}"
_STRUCTURAL_LABEL = (
    "minus STRUCTURAL_SLIDE_TYPES {hook, takeaway, sources, links, follow, repo}"
)
_REPO_LABEL = "minus repo only (prediction 3 check)"


def evaluate_predictions(means: dict[str, dict[str, float]]) -> list[dict]:
    """Check the three predictions D75 pre-registered BEFORE this script ran,
    against the actually-measured `means`. Returns one record per prediction
    with the raw numbers behind the verdict -- so the verdict is traceable to
    a number in the tables above, not asserted on its own authority. Whether
    each holds is reported "in those words" (T120's own requirement): if the
    measured direction or the measured magnitude band diverges from the
    pre-registered claim, that is recorded as a FAIL, not silently reworded
    into whatever the data happened to show.
    """
    cur, lst = means["golden_current"], means["golden_listonly"]
    records = []

    # Prediction 1: excluding PIPELINE_AUTHORED_TYPES moves grounding UP on
    # both sets, relative +0% to +7%.
    rel_cur = (cur[_PIPELINE_LABEL] - cur["as-shipped"]) / cur["as-shipped"] * 100
    rel_lst = (lst[_PIPELINE_LABEL] - lst["as-shipped"]) / lst["as-shipped"] * 100
    direction_ok = rel_cur > 0 and rel_lst > 0
    band_ok = (0 <= rel_cur <= 7) and (0 <= rel_lst <= 7)
    records.append(
        {
            "n": 1,
            "claim": "excluding PIPELINE_AUTHORED_TYPES moves grounding up on both "
            "sets, relative +0% to +7%",
            "direction_held": direction_ok,
            "magnitude_held": band_ok,
            "held": direction_ok and band_ok,
            "detail": f"golden_current {cur['as-shipped']:.4f} -> "
            f"{cur[_PIPELINE_LABEL]:.4f} ({rel_cur:+.1f}% relative); "
            f"golden_listonly {lst['as-shipped']:.4f} -> "
            f"{lst[_PIPELINE_LABEL]:.4f} ({rel_lst:+.1f}% relative)",
        }
    )

    # Prediction 2: excluding all of STRUCTURAL_SLIDE_TYPES moves grounding
    # DOWN, and further down on golden_listonly than on golden_current.
    delta_cur = cur[_STRUCTURAL_LABEL] - cur["as-shipped"]
    delta_lst = lst[_STRUCTURAL_LABEL] - lst["as-shipped"]
    direction_ok = delta_cur < 0 and delta_lst < 0
    further_ok = delta_lst < delta_cur  # "further down" = a more negative delta
    records.append(
        {
            "n": 2,
            "claim": "excluding all of STRUCTURAL_SLIDE_TYPES moves grounding down, "
            "and further down on golden_listonly than on golden_current",
            "direction_held": direction_ok,
            "magnitude_held": further_ok,
            "held": direction_ok and further_ok,
            "detail": f"golden_current {cur['as-shipped']:.4f} -> "
            f"{cur[_STRUCTURAL_LABEL]:.4f} (delta {delta_cur:+.4f}); "
            f"golden_listonly {lst['as-shipped']:.4f} -> "
            f"{lst[_STRUCTURAL_LABEL]:.4f} (delta {delta_lst:+.4f})",
        }
    )

    # Prediction 3: the golden_current vs golden_listonly gap NARROWS when
    # repo slides are excluded from both.
    gap_baseline = lst["as-shipped"] - cur["as-shipped"]
    gap_minus_repo = lst[_REPO_LABEL] - cur[_REPO_LABEL]
    narrowed = abs(gap_minus_repo) < abs(gap_baseline)
    records.append(
        {
            "n": 3,
            "claim": "the golden_current vs golden_listonly gap narrows when repo "
            "slides are excluded from both",
            "direction_held": narrowed,
            "magnitude_held": narrowed,
            "held": narrowed,
            "detail": f"gap as-shipped {gap_baseline:+.4f} -> "
            f"gap minus-repo {gap_minus_repo:+.4f}",
        }
    )
    return records


def print_predictions_verdict(means: dict[str, dict[str, float]]) -> None:
    print("=" * 72)
    print("PRE-REGISTERED PREDICTIONS (AC6) -- reported in the words they were")
    print("made in; a wrong prediction is recorded as a finding, not reworded")
    print("=" * 72)
    for record in evaluate_predictions(means):
        verdict = "HELD" if record["held"] else "DID NOT HOLD"
        print(f"  Prediction {record['n']}: {record['claim']}")
        print(f"    verdict: {verdict}")
        print(f"    measured: {record['detail']}")
        print()


if __name__ == "__main__":
    main()
