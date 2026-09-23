"""Re-harvest FILLER_PHRASES against non-closing slide text only (T74/D73).

The first harvest (T93/T74) scanned every slide field in the corpus,
including the mandatory closing slides `ensure_closing_slide`/
`ensure_links_slide` write themselves (`compose.py`). That measured the
pipeline's own repair text, not the model's writing (D70): 94% of the hits
were the pipeline's own CTA literals, and decks where the pipeline had to
insert its literal follow slide averaged 2.71 filler hits against 1.30 where
the model wrote its own closing content -- a ~2x penalty for a repair the
code performed, not a defect in the deck.

D73 fixes the scope: `filler_count` now excludes `PIPELINE_AUTHORED_TYPES`
slides (links/sources/follow) entirely, so this harvest re-verifies every
candidate phrase against exactly the same scan scope `filler_count` actually
uses -- the model-authored slide types only. A candidate whose only real
occurrences were on a closing slide (the four CTA literals chief among them,
D77) finds zero citations here and is dropped from the printed table, not
carried on the strength of its old (pre-partition) citation.

Every citation is a real, programmatic substring match against a `doc`-
validated (i.e. validated, post-processed -- not a failed row's raw
completion) deck's `_slide_text()` fields, the exact field set
`deck_body_text()` walks. Nothing here is invented; a candidate with no
citation prints as DROPPED, not silently omitted.

    ./.venv/bin/python scripts/harvest_filler_phrases.py

Exits non-zero, naming the problem, if data/eval/*.json (the gitignored eval
corpus) is not present -- never a silent empty table (D78-adjacent failure
mode: a script that "succeeds" while measuring nothing).
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline.quality import PIPELINE_AUTHORED_TYPES, _slide_text  # noqa: E402

EVAL_DIR = ROOT / "data" / "eval"

#: Every saved eval_compose.py RUN in the corpus, named explicitly rather
#: than a glob over EVAL_DIR -- `golden.json` (the golden set's item
#: DEFINITIONS: {set_version, cases}) lives in the same directory and is not
#: a run at all, so a glob would either crash on its shape or need its own
#: special case. Same eight files the first T74 harvest cited from.
RUN_FILES: tuple[str, ...] = (
    "golden_current", "golden_listonly", "golden_baseline", "golden_fewshot",
    "baseline", "fewshot", "e26_baseline", "e26_fewshot",
)

#: Candidate filler phrases, editorially chosen (generic CTA/engagement
#: boilerplate, generic section-header templates, generic hedge/preamble
#: prose) and VERIFIED below -- never included on the strength of sounding
#: generic alone. Carries the four pipeline-literal CTA phrases too
#: (D77: "follow for more", "daily tech, explained", "all the links",
#: "screenshot this slide") so the run PROVES they score zero non-closing
#: citations post-partition, rather than assuming it.
CANDIDATES: tuple[str, ...] = (
    # Generic call-to-action / engagement boilerplate.
    "follow for more",
    "daily tech, explained",
    "stay updated on",
    "screenshot this slide",
    "all the links",
    "get the latest on",
    "get updates on",
    "follow for updates on",
    # Generic section-header templates.
    "why it matters",
    "key details",
    "key takeaway",
    "key characteristics",
    "key impact metrics",
    "what went wrong",
    "what changed",
    "at a glance",
    "core tenets",
    # Generic hedge/preamble phrasing.
    "ideal for",
    "worth exploring",
    "worth studying",
    "the future of",
    "raises questions",
    "curated toolkit",
    "curated list of",
    "curated resources",
    "ranked by relevance",
    "curriculum for developers at any stage",
    "ready to clone",
)


def load_docs() -> list[tuple[str, object, dict]]:
    """(run file stem, item id, deck) for every doc-validated row in RUN_FILES.

    `doc` mirrors `scripts/eval_compose.py`'s `_deck_from_row`: a successful
    row persists its finished, validated deck at `doc`; a failed row's `raw`
    (its last unvalidated completion) is deliberately NOT read here -- a
    citation must come from a deck that actually shipped, not a rejected
    draft. Missing files are skipped rather than treated as an error; the
    CLI entry point below is what enforces "the corpus must exist at all".
    """
    docs: list[tuple[str, object, dict]] = []
    for name in RUN_FILES:
        path = EVAL_DIR / f"{name}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for row in data.get("rows", []):
            doc = row.get("doc")
            if isinstance(doc, dict) and doc.get("slides"):
                docs.append((name, row.get("id"), doc))
    return docs


def non_closing_fragments(doc: dict) -> list[tuple[str, str]]:
    """(slide type, fragment) for every prose fragment on a slide that is
    NOT one of `PIPELINE_AUTHORED_TYPES` -- the exact scan scope
    `filler_count` uses post-D73, so a citation found here is a citation
    `filler_count` can actually reach in production.
    """
    out: list[tuple[str, str]] = []
    for slide in doc.get("slides") or []:
        if not isinstance(slide, dict):
            continue
        slide_type = slide.get("type")
        if slide_type in PIPELINE_AUTHORED_TYPES:
            continue
        for fragment in _slide_text(slide):
            out.append((slide_type, fragment))
    return out


def harvest() -> dict[str, tuple[str, object, str]]:
    """Every candidate's first real citation. Missing = dropped."""
    docs = load_docs()
    citations: dict[str, tuple[str, object, str]] = {}
    fragment_count = 0
    for file_name, item_id, doc in docs:
        for slide_type, fragment in non_closing_fragments(doc):
            fragment_count += 1
            lowered = str(fragment).lower()
            for phrase in CANDIDATES:
                if phrase in citations:
                    continue
                if phrase in lowered:
                    citations[phrase] = (file_name, item_id, slide_type)
    return citations, len(docs), fragment_count


def main() -> int:
    if not EVAL_DIR.exists() or not any(EVAL_DIR.glob("*.json")):
        print(
            f"ERROR: no eval corpus at {EVAL_DIR} -- data/ is gitignored and "
            "this script needs the real, saved eval_compose.py runs to "
            "verify a phrase against. Nothing was harvested.",
            file=sys.stderr,
        )
        return 1

    citations, deck_count, fragment_count = harvest()

    print("harvest_filler_phrases.py -- configuration")
    print(f"  files scanned:      {', '.join(RUN_FILES)}")
    print("  validation:         doc-only (validated, post-processed decks; "
          "no raw/unvalidated completions)")
    print(f"  included types:     every slide type EXCEPT "
          f"{sorted(PIPELINE_AUTHORED_TYPES)} (D73's pipeline-authored set)")
    print(f"  date:               {datetime.date.today().isoformat()}")
    print(f"  decks scanned:      {deck_count}")
    print(f"  fragments scanned:  {fragment_count}")
    print()

    found = [p for p in CANDIDATES if p in citations]
    dropped = [p for p in CANDIDATES if p not in citations]
    print(f"{len(found)}/{len(CANDIDATES)} candidates found a real citation "
          f"in a non-closing slide; {len(dropped)} dropped\n")

    width = max(len(repr(p)) for p in CANDIDATES)
    for phrase in CANDIDATES:
        if phrase in citations:
            file_name, item_id, slide_type = citations[phrase]
            print(f"  {phrase!r:<{width}}  {file_name:<16} id={item_id!s:<4} "
                  f"{slide_type}")
        else:
            print(f"  {phrase!r:<{width}}  DROPPED -- no citation in any "
                  f"non-closing slide fragment")

    if dropped:
        print(f"\ndropped ({len(dropped)}): {dropped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
