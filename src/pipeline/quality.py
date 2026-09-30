"""Mechanical substance metrics for a composed deck.

The eval harness (`scripts/eval_compose.py`) has measured *shape* for a while
— 3 slides minimum, a hook first, a closing slide, no overlong headline — and
shape compliance is solved. The user's complaint ("posts produced by the free
models are very bad") is not about shape. It is about substance: generic
claims, no numbers, the same opener every run, text that could have been
written without reading the source article at all.

Every metric in this module is deterministic and reproducible from saved
output — string/regex/set arithmetic only. **No model ever judges another
model's output here.** A judge would share the same blind spots as the model
it is grading and return a number that feels like evidence without being one
(see `scripts/eval_compose.py`'s own docstring, and coding-standards.md rule
6). If a future change adds an LLM call anywhere in this file, that change is
wrong regardless of how well-intentioned it is.

Two building blocks feed all seven metrics:

  - `deck_body_text(doc)`  — every prose fragment on the deck's slides, by
    DEFAULT across every slide type including the ones the pipeline itself
    writes (`ensure_closing_slide`/`ensure_links_slide`'s CTA text, D70/D75)
    — NOT only "what the model composed", despite that being the intuitive
    reading of the name. links/urls/code/image fields are excluded (a URL is
    not prose, and `code_preserved` covers "code" on its own terms), but
    slide TYPE is not, by default. `filler_count` is the one caller that
    narrows scope, passing `exclude_types=PIPELINE_AUTHORED_TYPES` (D73) —
    `numeric_density`, `proper_noun_density` and `score_substance`'s
    `word_count` all still read the unscoped default, so pipeline-authored
    text still contributes to those three today (D75's general form of D70,
    unresolved for these three metrics; see `PIPELINE_AUTHORED_TYPES` and
    `MODEL_WRITTEN_MANDATED_TYPES` below for the exact partition).
  - `source_text_from_item(item)` — the brief plus every research note's
    claim + detail, i.e. everything the model was actually given to work
    from. `grounding_ratio` diffs the two.

`substance_composite()` folds all seven into one 0-100 number. The weighting
is explained where the weights are defined below — it is a first guess
pending real baseline data (T68's own `--rescore` run below IS that first
data point), not a fitted model. Revisit it once T75/T77 produce enough runs
to see whether any single term dominates the ranking.

`item_echo_ratio()` (T187) is an eighth function in this module but not an
eighth substance metric: it is reported-only, has no `COMPOSITE_WEIGHTS`
entry, and `substance_composite()` never reads it. It compares `item.brief`
against `item.raw_text` directly — a provenance tripwire for T188, not a
deck-quality signal. See its own docstring for the full rationale.
"""

from __future__ import annotations

import re
import unicodedata

#: Word tokenizer shared by every metric that needs one, so "what counts as a
#: word" is defined exactly once. Includes internal apostrophes/hyphens
#: ("don't", "GPT-5") so a hyphenated model name or contraction is one token,
#: not two plus noise.
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")

#: Naive sentence splitter: break after . ! or ? followed by whitespace. Good
#: enough for "was this token the first word of its sentence" — the one thing
#: proper_noun_density needs it for — without pulling in an NLP dependency for
#: a mechanical eval script.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

#: A version-like or product-like token: "3.13", "v2", "GPT-4o", "iOS-17".
#: Counted as a named entity regardless of sentence position, since a version
#: number is exactly the kind of specific detail this metric exists to find.
_VERSION_RE = re.compile(r"^[A-Za-z]*\d+(?:\.\d+)*[A-Za-z]*$")

#: Slide fields that carry the prose/labels a reader actually sees, across
#: every slide type in `pipeline.stages.compose.SLIDE_TYPES`. Deliberately
#: excludes "urls", "links", "image", "image_mode", "lang" and "type" — a URL
#: or a field the model didn't write prose into is not part of the substance
#: question these metrics ask. "code" is excluded from general text too: its
#: own fidelity is measured by `code_preserved`, and mixing a JSON blob into
#: prose-density counts would just add noise to numeric/proper-noun/filler
#: density. This is a distinct list from `eval_compose.py`'s BODY_FIELDS,
#: which answers a different question (is this slide non-empty at all).
_SCALAR_TEXT_FIELDS = ("sub", "quote", "attribution", "caption", "name",
                       "left_title", "right_title")


def _slide_text(slide: dict) -> list[str]:
    """Every prose fragment on one slide, as a flat list of strings."""
    parts: list[str] = []
    headline = slide.get("headline")
    if headline:
        parts.append(str(headline))
    for key in _SCALAR_TEXT_FIELDS:
        value = slide.get(key)
        if value:
            parts.append(str(value))
    bullets = slide.get("bullets")
    if isinstance(bullets, list):
        parts.extend(str(b) for b in bullets if b)
    stat = slide.get("stat")
    if isinstance(stat, dict):
        for key in ("value", "label"):
            if stat.get(key):
                parts.append(str(stat[key]))
    for row in slide.get("rows") or []:
        if isinstance(row, (list, tuple)):
            parts.extend(str(cell) for cell in row if cell)
    for step in slide.get("steps") or []:
        if isinstance(step, dict):
            for key in ("label", "detail"):
                if step.get(key):
                    parts.append(str(step[key]))
    for tile in slide.get("tiles") or []:
        if isinstance(tile, dict):
            for key in ("value", "label", "delta"):
                if tile.get(key):
                    parts.append(str(tile[key]))
    for entry in slide.get("series") or []:
        if isinstance(entry, dict):
            for key in ("label", "display"):
                if entry.get(key):
                    parts.append(str(entry[key]))
    return parts


def _slides(doc: dict) -> list[dict]:
    """`doc`'s slide list, dict entries only.

    `--rescore` reads a saved run's `raw` field for a row where compose
    failed — the model's last completion before any validation ever touched
    it. A free model producing a malformed shape (a slide that is a string,
    or a "slides" key that is not a list at all) is exactly the kind of thing
    `BadCompletion`/`_is_provider_fault` exist to handle elsewhere in the
    pipeline; a metric reading that same raw completion off disk must not
    crash on it either. Anything that is not a dict is silently dropped
    rather than scored as if it were a slide.

    `doc` itself is guarded too, not just `doc["slides"]` — `_deck_from_row`
    shields every call `--rescore` makes, but `score_substance` has no such
    shield when called directly on a fresh compose result (T75), so `None`,
    a list, or a bare string must degrade to "no slides", not raise.
    """
    if not isinstance(doc, dict):
        return []
    raw = doc.get("slides")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def deck_body_text(doc: dict, *, exclude_types: frozenset[str] = frozenset()) -> str:
    """Every prose fragment in the deck, in slide order, one blob.

    Each fragment (a headline, a sub, one bullet, one table cell, one KPI
    label...) is sentence-terminated before joining. Slide fields are
    routinely written without trailing punctuation -- a headline, a table
    cell, a KPI label never end in a period -- so without this,
    `proper_noun_density`'s "not the first word of its sentence" rule would
    spuriously treat the first word of the NEXT field as mid-sentence just
    because the previous field happened to lack one: an artifact of how the
    fragments were concatenated, not of anything the model wrote.

    `exclude_types` drops whole slides of the given type(s) before their
    fragments are ever produced. Nothing else in this module passes it
    (every other caller sees exactly the slides it always has); `filler_count`
    is the only caller that does, so it can skip `PIPELINE_AUTHORED_TYPES`
    slides (D73) -- text `ensure_closing_slide`/`ensure_links_slide` write,
    not the model.
    """
    parts: list[str] = []
    for slide in _slides(doc):
        if slide.get("type") in exclude_types:
            continue
        parts.extend(_slide_text(slide))
    terminated = [
        p if p.rstrip().endswith((".", "!", "?")) else p.rstrip() + "."
        for p in parts if p and p.strip()
    ]
    return " ".join(terminated)


def source_text_from_item(item) -> str:
    """The brief plus every research note's claim + detail.

    This is what the model was actually handed. `grounding_ratio` measures
    how much of the deck's language traces back to it.
    """
    parts = [item.brief or ""]
    for note in item.research or []:
        parts.append(str(note.get("claim") or ""))
        parts.append(str(note.get("detail") or ""))
    return " ".join(p for p in parts if p)


def hook_headline(doc: dict) -> str:
    """The deck's hook headline, or "" if the deck has none (a shape defect
    `score_deck` already flags — this just must not crash on it)."""
    for slide in _slides(doc):
        if slide.get("type") == "hook":
            return str(slide.get("headline") or "")
    return ""


# --------------------------------------------------------------- metric 1


def numeric_density(doc: dict) -> float:
    """Figures per 100 words of deck body text.

    "Numbers beat adjectives" is already in the compose SYSTEM prompt
    (`pipeline/stages/compose.py`) — this measures obedience to it. A
    "figure" is any word token containing at least one digit: "92%", "$65B",
    "2026", "4.2s" all count; "ninety-two" does not, because the model was
    told to write the number, not the word.
    """
    words = _WORD_RE.findall(deck_body_text(doc))
    if not words:
        return 0.0
    figures = sum(1 for w in words if any(c.isdigit() for c in w))
    return round(figures / len(words) * 100, 2)


# --------------------------------------------------------------- metric 2


def proper_noun_density(doc: dict) -> float:
    """Named-entity-like tokens per 100 words of deck body text.

    Mechanical, not NLP: a token counts when it is either (a) version/product
    shaped ("GPT-4o", "v3.13" — regardless of position), (b) an all-caps
    acronym of 2+ letters ("API", "MMLU" — regardless of position, since an
    acronym reads as a proper noun wherever it lands), or (c) capitalised and
    NOT the first word of its sentence. (c) is the deliberate design choice:
    every sentence starts with a capital letter whether or not the subject is
    a named entity, so counting sentence-initial capitals would just measure
    sentence count. Counting only the *interior* capitals is what turns this
    into a specificity signal instead of a grammar signal — at the cost of
    undercounting entities that happen to open a sentence, which is an
    accepted trade for not overcounting every sentence there is.
    """
    text = deck_body_text(doc)
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    hits = 0
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        tokens = _WORD_RE.findall(sentence)
        for i, tok in enumerate(tokens):
            if _VERSION_RE.match(tok) and any(c.isdigit() for c in tok):
                hits += 1
            elif tok.isupper() and len(tok) > 1:
                hits += 1
            elif i > 0 and tok[0].isupper() and not tok.isupper():
                hits += 1
    return round(hits / len(words) * 100, 2)


# --------------------------------------------------------------- metric 3


def _fold_apostrophes(text: str) -> str:
    """NFKC-normalise `text` and fold the typographic apostrophe (U+2019) to
    the ASCII one `_WORD_RE` actually matches.

    LLM output routinely writes "Apple’s" with a curly apostrophe; briefs and
    research notes routinely write "Apple's" with the ASCII one. `_WORD_RE`'s
    character class includes `'` (U+0027) but not `’` (U+2019), so without
    this fold the two spellings of the identical word tokenise differently
    ("apple's" as one token vs "apple" + "s" as two) — shifting every 3-gram
    downstream of the split and deflating `grounding_ratio` on a sentence
    that is, word for word, the same as the source.
    """
    return unicodedata.normalize("NFKC", text).replace("’", "'")


def _ngrams(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD_RE.findall(_fold_apostrophes(text))]
    if len(words) < n:
        return set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def grounding_ratio(doc: dict, source_text: str) -> float:
    """Share of the deck's word 3-grams that also occur in the source text.

    This is the direct measure of "generic claims": a phrase the model could
    have written without reading anything shares no 3-gram with the brief or
    research notes, while a specific, sourced claim tends to reuse the
    source's own wording for at least a few words at a time. Unique 3-grams
    on both sides (a set, not a multiset), so a deck that repeats one grounded
    phrase five times cannot inflate the ratio past what a single use of it
    would score.

    The deck side is built **per fragment** — the union of `_ngrams` over
    each individual fragment `_slide_text` returns, not `_ngrams` of the
    joined `deck_body_text` blob. Joining fragments with ". " before
    tokenising (as `deck_body_text` does, for `proper_noun_density`'s
    sentence splitter) forges 3-grams that straddle the boundary between two
    adjacent fragments — the last word of one and the first two of the next —
    that exist in neither the deck's actual prose nor, generally, the source.
    Those artifacts track fragment count, not grounding, and can manufacture
    a nonzero score for a deck that shares nothing real with the source at
    all (see `test_grounding_ratio_does_not_invent_grams_at_fragment_joins`).
    Scoring per fragment and unioning the results cannot produce a 3-gram
    that never appeared within a single fragment as the model wrote it.

    A deck with fewer than 3 words of body text has no 3-grams to check and
    scores 0.0 — that deck has a much larger problem than grounding, and 0.0
    correctly reports "no evidence of grounding" rather than raising.
    """
    deck_grams: set[tuple[str, ...]] = set()
    for slide in _slides(doc):
        for fragment in _slide_text(slide):
            deck_grams |= _ngrams(str(fragment))
    if not deck_grams:
        return 0.0
    source_grams = _ngrams(source_text or "")
    if not source_grams:
        return 0.0
    return round(len(deck_grams & source_grams) / len(deck_grams), 4)


# ------------------------------------------------------- reported only (T187)


def item_echo_ratio(brief: str | None, raw_text: str | None) -> float:
    """Share of the brief's word 3-grams that also occur in the item's
    original raw text -- how much of the brief is the item talking, versus
    the research.

    T187 / D156: `pipeline.stages.synthesize` (T188) will let the brief draw
    on `item.raw_text` as an attributed source. `grounding_ratio` cannot see
    that happen: `source_text_from_item` is `item.brief` plus every research
    note's `claim`/`detail` -- `item.raw_text` is NOT one of its fields, so
    `grounding_ratio` stays mechanically untouched (same function, same
    value on every recorded run, same four frozen inputs, house rule 12).
    But `item.brief` IS one of `source_text_from_item`'s fields, so once the
    brief may legitimately carry item-derived sentences, `raw_text` gets
    laundered into "grounded" status through the brief -- a deck phrase
    tracing to the item's own unverified claim starts counting as sourced.
    This metric is the one place in the repo that can see that laundering
    happen; nothing else here reads `item.raw_text` against `item.brief` at
    all.

    Field-set + authorship (house rule 11): `brief` (`item.brief`) is
    MODEL-WRITTEN -- produced by a `synthesize` LLM call. `raw_text`
    (`item.raw_text`) is THIRD-PARTY: the original Telegram channel post,
    forwarded message, or operator DM, written by the intake layer
    (`intake/channel.py`, `intake/bot_intake.py`, `intake/manual.py`) before
    the pipeline ever runs, and never reassigned by any pipeline STAGE --
    every stage that touches `item.raw_text` (compose, synthesize, research,
    triage, enumerate_items, extract, conversation) only reads it. Unlike
    `_restore_repo_facts`'s interaction with `grounding_ratio` (house rule
    11's own worked example -- the PIPELINE copying its own research text
    into a slide, which can inflate a metric reading what the pipeline just
    wrote), there is no self-inflation path here: nothing in this repo ever
    writes to `raw_text` after intake, so this ratio cannot be moved by the
    pipeline second-guessing itself.

    Reported only, never a `substance_composite` term -- same treatment
    `filler_count` got at v3 (D74): no entry in `COMPOSITE_WEIGHTS`, and
    `substance_composite` never reads this key even if a caller's `metrics`
    dict happens to carry one. A composite is a quality judgement; this is a
    provenance tripwire for a NEW failure mode T188 can introduce (the brief
    degenerating into a paraphrase of the post, with the research doing
    nothing) -- worth watching, never worth rewarding or punishing a score
    over.

    An empty (or sub-3-word) `brief` has no 3-grams to compare and scores
    0.0 -- "no evidence of echo either way", the same empty-denominator
    convention `grounding_ratio` already uses, not a raised
    `ZeroDivisionError`: a metric that crashes on the empty-brief edge case
    is how a harness starts silently skipping exactly the items most likely
    to already be a defect. An empty (or sub-3-word) `raw_text` scores 0.0
    the same way, for the same reason, on the other side of the ratio. A
    brief that is a verbatim copy of `raw_text` scores 1.0: every brief
    3-gram is then also a `raw_text` 3-gram by construction.

    Does not read, call, or re-derive any of house rule 12's four frozen
    `grounding_ratio` inputs (`deck_body_text`/`_slide_text`'s field set,
    `_fold_apostrophes`, `_ngrams`'s `n`, `source_text_from_item`'s field
    set) -- it takes `brief` as a plain string argument and reuses `_ngrams`
    exactly as written, the same read-only reuse `grounding_ratio` itself
    makes of that helper. It does not touch `doc`/slides at all: this is a
    property of the brief and the item, not of the composed deck.
    """
    brief_grams = _ngrams(brief or "")
    if not brief_grams:
        return 0.0
    raw_grams = _ngrams(raw_text or "")
    if not raw_grams:
        return 0.0
    return round(len(brief_grams & raw_grams) / len(brief_grams), 4)


# --------------------------------------------------------------- metric 4


def hook_similarity(hooks: list[str]) -> float:
    """Mean, over every hook, of its highest Jaccard similarity to any other
    hook in the list — a run-level metric, not a per-deck one.

    The same opener every time is a real, user-visible failure mode: a user
    scrolling several posts in a row sees "Big news in AI today" open every
    single one. Jaccard is computed over each hook's lowercased word set.
    A run of fewer than 2 hooks has nothing to compare and scores 0.0.
    1.0 means every hook has an exact word-set duplicate somewhere in the run.
    """
    tokenised = [set(_WORD_RE.findall(h.lower())) for h in hooks if h]
    if len(tokenised) < 2:
        return 0.0
    best_per_hook = []
    for i, a in enumerate(tokenised):
        best = 0.0
        for j, b in enumerate(tokenised):
            if i == j:
                continue
            union = a | b
            if not union:
                continue
            best = max(best, len(a & b) / len(union))
        best_per_hook.append(best)
    return round(sum(best_per_hook) / len(best_per_hook), 4)


# --------------------------------------------------------------- metric 5


#: Slide types `ensure_closing_slide` / `ensure_links_slide`
#: (`pipeline.stages.compose`) write themselves, unconditionally, whatever
#: the model produced -- one mandatory two-line CTA and one numbered index,
#: always. `filler_count` excludes exactly this set (D73), because a metric
#: whose docstring says it measures "what the model composed" is false by
#: construction on a slide the pipeline wrote: 94% of the pre-D73 hits were
#: this text, and a deck was penalised ~2x for a repair the code performed
#: (D70). `bare_bullet_ratio` does not exclude only this set -- see
#: `MODEL_WRITTEN_MANDATED_TYPES` below.
PIPELINE_AUTHORED_TYPES: frozenset[str] = frozenset({"links", "sources", "follow"})

#: Slide types the pipeline MANDATES (every deck opens on a hook and closes
#: with a takeaway; a "repo" card is populated straight from research) but
#: does NOT write the prose for -- the model writes the headline/sub/detail,
#: the pipeline only enforces that the slide exists and, for "repo", that
#: its facts (owner/name/url/stars/language) are correct. `filler_count`
#: does NOT exclude these: they are where the ~7 genuinely model-authored
#: hits in this corpus live ("8 GitHub repos worth studying", "Curated
#: toolkit covering...") -- the closest thing here to the user's original
#: "generic AI slop" complaint (D73).
MODEL_WRITTEN_MANDATED_TYPES: frozenset[str] = frozenset({"hook", "takeaway", "repo"})

#: The union -- unchanged in VALUE from the single set this module used to
#: define directly (six types: hook, takeaway, sources, links, follow,
#: repo), now stated as a composition of the two partitions above so
#: `bare_bullet_ratio` (which excludes the whole union, D70's rationale
#: still applies to it unchanged) and `filler_count` (which excludes only
#: `PIPELINE_AUTHORED_TYPES`) read their shared boundary off the same two
#: named halves instead of one restating the other.
STRUCTURAL_SLIDE_TYPES: frozenset[str] = (
    PIPELINE_AUTHORED_TYPES | MODEL_WRITTEN_MANDATED_TYPES
)


#: Blocklist of generic filler phrases that read as padding rather than
#: substance ("in today's fast-paced world", "game-changer", "stay tuned" —
#: the kind of line that could sit on any post about any subject).
#:
#: T74 re-harvest (2026-09-23, `scripts/harvest_filler_phrases.py`, run
#: against `data/eval/{golden_current,golden_listonly,golden_baseline,
#: golden_fewshot,baseline,fewshot,e26_baseline,e26_fewshot}.json`): every
#: entry below is a literal (case-insensitive) substring of a `doc`-validated
#: deck's `_slide_text()` fields on a slide OUTSIDE `PIPELINE_AUTHORED_TYPES`
#: — the exact scope `filler_count` scans (D73). The full phrase -> file ->
#: item id -> slide-type citation table is the script's own printed output;
#: every entry is reproducible by re-running it, not by trusting this
#: comment or a task log.
#:
#: This supersedes the first T93/T74 harvest, which scanned every slide type
#: including the mandatory `links`/`sources`/`follow` closing slides.
#: **9 of that harvest's 28 candidates found NO citation once closing slides
#: are excluded and were dropped, not kept on their old citation: 4 + 4 + 1
#: = 9** — the four literals `ensure_closing_slide`/`ensure_links_slide`
#: write verbatim ("follow for more", "daily tech, explained", "all the
#: links", "screenshot this slide"), four near-identical model-written CTA
#: phrasings for the same closing role ("stay updated on", "get the latest
#: on", "get updates on", "follow for updates on"), and one hedge phrase
#: whose only occurrence happened to be on a `follow` slide ("the future
#: of"). D77: an entry that can never fire misstates what the metric
#: measures, so it leaves the list rather than staying as a documented dead
#: weight.
#: Honest finding from BOTH harvests, not assumed going in: this golden set
#: is unusually factual/curated (numbers, quotes, named sources), so classic
#: hype filler ("game-changer", "in today's fast-paced world") essentially
#: never occurs in it (D64) — the real, measured filler that survives
#: exclusion of the pipeline's own closing slides is (a) generic
#: section-header templates ("Why it matters", "Key details") reused
#: verbatim across unrelated topics, and (b) generic hedge/preamble phrasing
#: in body and hook prose. Two wrong conclusions have already shipped in
#: this programme from guessing instead of measuring (D9, D27) — this list
#: is deliberately what was FOUND, not what "generic AI slop" was assumed to
#: look like.
FILLER_PHRASES: tuple[str, ...] = (
    # Generic section-header templates — the identical header recurs across
    # decks about unrelated topics (chess, OKF, MCP, neo-Nazi networks...).
    "why it matters",  # golden_fewshot id=10 takeaway
    "key details",  # golden_listonly id=3 facts
    "key takeaway",  # golden_fewshot id=22 takeaway
    "key characteristics",  # golden_current id=14 point
    "key impact metrics",  # golden_current id=20 kpi
    "what went wrong",  # golden_listonly id=10 point
    "what changed",  # golden_baseline id=8 point ("What changed?")
    "at a glance",  # golden_baseline id=5 facts
    "core tenets",  # golden_listonly id=37 point
    # Generic hedge/preamble phrasing found in body prose — conveys no
    # subject-specific information, interchangeable across any listicle/story.
    "ideal for",  # golden_baseline id=30 repo
    "worth exploring",  # golden_listonly id=29 hook
    "worth studying",  # golden_current id=29 hook
    "raises questions",  # golden_listonly id=15 takeaway
    "curated toolkit",  # golden_listonly id=26 hook
    "curated list of",  # golden_baseline id=29 point
    "curated resources",  # golden_current id=30 hook
    "ranked by relevance",  # golden_current id=26 hook
    "curriculum for developers at any stage",  # golden_listonly id=30 hook
    "ready to clone",  # golden_baseline id=30 hook
)


def filler_count(doc: dict, phrases: tuple[str, ...] | None = None) -> int:
    """Case-insensitive substring hits against a filler-phrase blocklist,
    scanning only slides the model itself is responsible for.

    `phrases` defaults to `None`, resolved to the CURRENT `FILLER_PHRASES`
    inside the function body rather than bound as a parameter default —
    `def filler_count(doc, phrases=FILLER_PHRASES)` would capture whatever
    `FILLER_PHRASES` was AT IMPORT TIME, so a later runtime reassignment of
    `quality.FILLER_PHRASES` (exactly what T74 did, and exactly what a test
    or a future re-harvest does again) would be invisible to every call that
    relies on the default — `score_substance` would keep reporting
    `filler_count=0` from the stale binding forever (T93/D26). The
    explicit-override path stays: it is what lets `filler_count`'s own
    counting logic be tested independently of whatever the module constant
    currently holds.

    `PIPELINE_AUTHORED_TYPES` slides are excluded from the scan (D73): the
    first harvest scored `ensure_closing_slide`/`ensure_links_slide`'s own
    literal text on 94% of its hits (D70) — a metric that reads its own
    pipeline's repair text back and calls that "the model's filler" is
    measuring the pipeline, not the model, and penalised a deck ~2x for a
    repair the code performed rather than a defect the model produced.
    """
    if phrases is None:
        phrases = FILLER_PHRASES
    if not phrases:
        return 0
    text = deck_body_text(doc, exclude_types=PIPELINE_AUTHORED_TYPES).lower()
    return sum(text.count(p.lower()) for p in phrases if p)


# --------------------------------------------------------------- metric 6


#: Slide types the compose prompt calls richer than headline-and-bullets —
#: each demands something structured and specific (a real code snippet, a
#: genuine A-vs-B, an ordered sequence, comparable figures, an actual quoted
#: statement) rather than paraphrased prose.
RICH_SLIDE_TYPES: frozenset[str] = frozenset(
    {"code", "flow", "compare", "kpi", "chart", "quote"}
)


def bare_bullet_ratio(doc: dict) -> float:
    """Share of content slides (excluding mandatory bookends) that are NOT
    one of the richer types the compose prompt names.

    A deck with no content slides at all (only bookends) has nothing to
    judge on this axis and scores 0.0 — that failure is caught by
    `shape_score`/`empty_slides` in `scripts/eval_compose.py`, not here.

    Excludes the WHOLE `STRUCTURAL_SLIDE_TYPES` union (defined above in
    metric 5) unlike `filler_count`, which excludes only
    `PIPELINE_AUTHORED_TYPES`. That is a deliberate, D73-recorded asymmetry,
    not an oversight: a `hook`/`takeaway`/`repo` slide's TYPE is mandated
    (one hook opens every deck, one takeaway closes the body, "repo" is
    populated straight from research) even though a model wrote its prose,
    so a bareness verdict on it would still be judging deck SHAPE rather
    than the model's choice between padding with bullets and reaching for
    something richer -- the one the compose prompt calls out by name as
    "the failure mode". Excluding it from `filler_count`'s scan too would
    instead throw away that metric's only real signal (D73 rejected that
    explicitly).
    """
    types = [s.get("type") for s in _slides(doc)]
    content = [t for t in types if t not in STRUCTURAL_SLIDE_TYPES]
    if not content:
        return 0.0
    bare = sum(1 for t in content if t not in RICH_SLIDE_TYPES)
    return round(bare / len(content), 4)


# --------------------------------------------------------------- metric 7


_FENCE_RE = re.compile(r"```[A-Za-z0-9_\-]*\n?(.*?)```", re.DOTALL)

#: How much of a fenced block's distinct tokens must reappear in a "code"
#: slide for the block to count as preserved. Below 1.0 on purpose — the
#: model is allowed to trim whitespace/comments or add a caption without
#: that counting as loss, since `compose.py`'s own instruction only demands
#: it stay verbatim source material, not a byte-identical copy.
_CODE_OVERLAP_THRESHOLD = 0.5


def code_preserved(doc: dict, brief: str | None) -> bool | None:
    """Did a fenced block in the brief survive into a "code" slide?

    Returns None (not applicable) when the brief carried no fenced block at
    all — most stories have no code to preserve, and scoring those as a
    failure would just add noise `substance_composite` then has to filter
    back out. `substance_composite` treats None as "exclude and renormalise
    the remaining weights", not as zero.
    """
    blocks = _FENCE_RE.findall(brief or "")
    if not blocks:
        return None
    code_slides = [
        str(s.get("code") or "")
        for s in _slides(doc)
        if s.get("type") == "code"
    ]
    if not code_slides:
        return False
    for block in blocks:
        block_tokens = set(_WORD_RE.findall(block.lower()))
        if not block_tokens:
            continue
        for code in code_slides:
            code_tokens = set(_WORD_RE.findall(code.lower()))
            if not code_tokens:
                continue
            overlap = len(block_tokens & code_tokens) / len(block_tokens)
            if overlap >= _CODE_OVERLAP_THRESHOLD:
                return True
    return False


# ------------------------------------------------------------- composite


def score_substance(item, doc: dict) -> dict:
    """The six per-deck raw metrics (all but `hook_similarity`, which is a
    run-level property — see `hook_similarity()`), plus `word_count` so
    `substance_composite` can turn `filler_count` into a density without
    retokenising the deck, plus `metric_set_version` (see `METRIC_SET_VERSION`)
    so a saved run carries, on every row, which composite era it belongs to —
    a reader does not have to cross-reference `FILLER_PHRASES`' current length
    against the date a run was saved to know whether it is comparable to
    another one (D15/D23).

    `grounding_ratio` and every other raw per-metric value returned here are
    UNCHANGED by which `METRIC_SET_VERSION` is active — only `filler_count`'s
    role in `substance_composite`'s weighted sum moves at that boundary (see
    there). A `grounding_ratio` of 0.30 means the same thing in v1, v2 and v3.

    `word_count` is scoped to `exclude_types=PIPELINE_AUTHORED_TYPES` — the
    SAME scope `filler_count`'s own numerator scans (D73) — not the full
    deck's word count. This field exists for exactly one purpose
    (`substance_composite`'s `filler_density = filler_count / word_count`),
    so numerator and denominator must count words over the same slides.
    Before this fix they did not: `filler_count` excluded
    `PIPELINE_AUTHORED_TYPES` text but `word_count` counted every slide,
    including the pipeline's own CTA slides — a full deck routinely carries
    more words in `follow`/`links`/`sources` than a filler hit could ever
    land in, so the mismatch under-reported density (measured, T74 bounce 1:
    scoped/full word count averages 0.906 of the unscoped count across the
    same 48-deck corpus B1 measured, min 0.543 — up to 46% under-reported on
    the worst deck). Inert while `FILLER_COUNT_IN_COMPOSITE` is False (see
    there), but the re-entry criterion is stated in this same density, so it
    must be correct before that flag is ever flipped, not merely documented
    as skewed.
    """
    text = deck_body_text(doc, exclude_types=PIPELINE_AUTHORED_TYPES)
    word_count = len(_WORD_RE.findall(text)) or 1
    return {
        "numeric_density": numeric_density(doc),
        "proper_noun_density": proper_noun_density(doc),
        "grounding_ratio": grounding_ratio(doc, source_text_from_item(item)),
        "filler_count": filler_count(doc),
        "bare_bullet_ratio": bare_bullet_ratio(doc),
        "code_preserved": code_preserved(doc, item.brief),
        "word_count": word_count,
        "metric_set_version": METRIC_SET_VERSION,
    }


#: Density (per 100 words) at which numeric_density / proper_noun_density
#: saturate to a full 1.0 component. First guesses, not fitted: roughly "one
#: figure every 12 words" and "one named entity every 7 words" read as
#: comfortably specific prose without demanding a spec sheet. Revisit once
#: `--rescore` has been run against more than the two golden sets.
NUMERIC_DENSITY_TARGET = 8.0
PROPER_NOUN_DENSITY_TARGET = 15.0

#: Filler hits (per 100 words) at which the filler_count term saturates to a
#: full 1.0 penalty. Reported alongside `filler_count` in `score_substance()`
#: for whenever `FILLER_COUNT_IN_COMPOSITE` is re-admitted (see there) —
#: inert today the same way it was inert pre-T74, just for a different
#: reason (the term is out of the composite by policy, not because the
#: blocklist is empty).
FILLER_DENSITY_TARGET = 3.0

#: Whether `filler_count` participates in `substance_composite` at all.
#: **False as of T74/D74 — a deliberate, permanent policy, not a stand-in
#: for "FILLER_PHRASES happens to be empty".** v1/v2 excluded the term only
#: incidentally, via `if FILLER_PHRASES` (empty blocklist -> excluded); this
#: constant replaces that check so the exclusion reads as a stated decision
#: regardless of the blocklist's size.
#:
#: **Measured** (T74 bounce 1, `filler_count()` itself over the 48
#: doc-or-raw-scored rows of `golden_current` + `golden_listonly` — the same
#: 48 rows `--rescore`'s own `decks_scored` counts across those two files):
#: 16 hits (7 + 9), 12 distinct phrases firing, on 15/48 decks (31%). The
#: original "~7 hits across 48 decks" was `golden_current`'s own count
#: relabelled as the two-file total — a half-measured number (only one of
#: the two files had actually been counted).
#:
#: What that supports is not "flat ~1.0 almost everywhere with near-zero
#: discrimination" — the composite's filler component (`1.0 -
#: saturate(filler_density, FILLER_DENSITY_TARGET)`, `word_count` scoped per
#: N2/T74 bounce 1 below) has mean 0.816, population stdev 0.335, min 0.0
#: across the same 48 decks, and 33/48 (69%) sit at the 1.0 ceiling. There is
#: real per-deck spread; the reason to keep the term OUT is the re-entry
#: criterion below, not a claim that the term has nothing left to
#: discriminate.
#: `filler_count` is still computed and reported RAW by `score_substance()`
#: — this only controls the composite.
#: **Re-entry criterion (D64), re-verified on the same 48 decks with the
#: corrected (scoped) `word_count`: 3/48 (6.25%) reach density >=
#: `FILLER_DENSITY_TARGET`** — below the criterion, so the decision to keep
#: this term out is UNCHANGED, but it is no longer "0/48": scoping
#: `word_count` (N2 fix) shrinks the denominator, which raises density on
#: every deck that has any filler at all. Flip to True, and give it a
#: `COMPOSITE_WEIGHTS` entry again, once a real run shows filler density >=
#: `FILLER_DENSITY_TARGET` on >= 10% of decks. This golden set is unusually
#: factual/curated, so filler's absence here is not evidence of its absence
#: in production — not before that measurement, and not as a drive-by.
FILLER_COUNT_IN_COMPOSITE: bool = False

#: Bumped whenever the composite's ACTIVE weight set changes shape (a term's
#: applicability flips, one is added/removed/reweighted) — not on every code
#: change. Exists so a saved run (or a person reading two numbers side by
#: side) can tell which composite era a figure belongs to without
#: re-deriving it from `FILLER_PHRASES`' current length (D15).
#:   v1 — FILLER_PHRASES was the empty T74 stub: filler_count always excluded
#:        and renormalised (5 applicable terms / 0.80 total weight, given
#:        code_preserved is also None on every deck in both golden sets — D22).
#:   v2 — T74 (2026-09-22): FILLER_PHRASES is populated, so filler_count was
#:        included whenever it was actually measured (6 applicable terms /
#:        0.90 total weight, same code_preserved caveat). See D23: composites
#:        stamped v1 and v2 are NOT comparable even though nothing about
#:        grounding_ratio or the other raw metrics changed. SUPERSEDED by v3
#:        (D69/D70): v2's measured numbers were ~90% the pipeline's own CTA
#:        text, not the model's.
#:   v3 — T74 re-scope (2026-09-23, D73/D74): `filler_count` is scoped to
#:        exclude `PIPELINE_AUTHORED_TYPES` slides, and now PERMANENTLY
#:        leaves the composite (`FILLER_COUNT_IN_COMPOSITE = False`) rather
#:        than being conditionally excluded. Arithmetically identical to v1
#:        (same 5 active terms, same 0.80 total weight, given code_preserved
#:        is still N/A on every golden deck) — D22's 39.570/42.542 reproduce
#:        to two decimals under v3.
METRIC_SET_VERSION: int = 3

#: Composite weights, fractions of 100. A first guess pending baseline data
#: — T68's own `--rescore` runs against the golden sets ARE that first data
#: point, nothing here is fit to a dataset yet. No `filler_count` entry as
#: of v3 (D74): it is not a candidate the composite renormalises around
#: (like `code_preserved` when N/A) -- it has no weight to renormalise
#: because it is not a composite term at all while `FILLER_COUNT_IN_COMPOSITE`
#: is False. Re-add its entry only alongside flipping that constant back on.
#:
#: grounding_ratio carries the most weight because it is the direct,
#: unambiguous measure of the reported complaint ("generic claims") taken
#: straight from the project brief in project-context.md — it is the one
#: metric that cannot be gamed by a deck that merely sounds specific.
#: numeric_density and bare_bullet_ratio get the next tier: both are named,
#: literal instructions in the compose SYSTEM prompt ("numbers beat
#: adjectives", "a deck of nothing but headline-and-bullets is the failure
#: mode"), so obeying them is worth real weight, but obeying an instruction
#: is weaker evidence of substance than grounding is. proper_noun_density is
#: a softer version of the same idea as numeric_density (specific nouns
#: rather than specific numbers) and gets a smaller share for it.
#: hook_similarity is real but narrower — a visible symptom of low-effort
#: generation rather than the substance question itself — so it gets the
#: smallest share. code_preserved is scoped to the minority of decks whose
#: source even contains a fenced block, so when it does not apply its weight
#: is redistributed across whichever terms do (see `substance_composite`),
#: rather than penalising every codeless deck for having nothing to preserve.
#:
#: These are the *documented* weights, not necessarily the *effective* ones:
#: `code_preserved` is `None` on every deck in both golden sets on disk
#: today (neither brief carries a fenced block), so every `--rescore` run so
#: far renormalised across the other five terms — the effective weight set
#: is 0.80 total, not the 0.90 this table nominally sums to.
COMPOSITE_WEIGHTS: dict[str, float] = {
    "grounding_ratio": 0.30,
    "numeric_density": 0.15,
    "bare_bullet_ratio": 0.15,
    "proper_noun_density": 0.10,
    "hook_similarity": 0.10,
    "code_preserved": 0.10,
}


def active_component_weights() -> dict[str, float]:
    """`COMPOSITE_WEIGHTS`, as a defensive copy.

    `filler_count` has no entry to filter here as of v3 (D74) — it left the
    composite for good, not conditionally on `FILLER_PHRASES`' size, so
    there is nothing left for this function to decide. It stays a function
    (rather than callers reading `COMPOSITE_WEIGHTS` directly) so a future
    per-deck-applicable term can be filtered here the same way
    `code_preserved` is documented but not filtered here (see below), without
    every existing caller needing to change.

    `code_preserved` is deliberately NOT filtered here even though it is
    `None` (excluded) on every deck in both golden sets today — its
    applicability is per-deck (does THIS brief carry a fenced block?), so no
    module-level answer is correct for every deck a caller might score next.
    It stays in this table at its documented weight; `substance_composite`'s
    own per-deck renormalisation is still what actually applies during
    scoring. See `METRIC_SET_VERSION` for the version this table's shape
    corresponds to.
    """
    return dict(COMPOSITE_WEIGHTS)


def _saturate(value: float, target: float) -> float:
    """Map a raw per-100-words density to 0-1, saturating at `target`."""
    if target <= 0:
        return 0.0
    return max(0.0, min(1.0, value / target))


def substance_composite(metrics: dict, hook_similarity: float = 0.0) -> float:
    """Combine the seven substance metrics into one 0-100 score.

    `metrics` is `score_substance()`'s return value (or anything with the
    same keys). `hook_similarity` is a separate argument, not a key of
    `metrics`, because it is a run-level property (see `hook_similarity()`
    above) — the caller computes it once across every hook in the run and
    passes the same value in for every deck in that run. It defaults to 0.0
    (no penalty) so a single deck can still be scored with no run context.

    `code_preserved` is excluded from the weighted sum, and the remaining
    weights renormalised, whenever it is None (the source had no fenced
    block to preserve) — see `code_preserved()`.

    `filler_count` is excluded from the weighted sum UNCONDITIONALLY as of
    v3 (see `FILLER_COUNT_IN_COMPOSITE`) — not renormalised in because it is
    N/A on a particular deck (like `code_preserved`), but because it is not
    a composite term at all right now, by policy. `filler_density` below is
    still computed from `metrics` on every call so the branch is cheap to
    re-enable, but it is not evaluated into a component value while
    `FILLER_COUNT_IN_COMPOSITE` is False.

    **The v1/v2/v3 history (D15/D23/D69/D74, METRIC_SET_VERSION):** v2 (T74,
    2026-09-22) included `filler_count` once `FILLER_PHRASES` stopped being
    empty, moving the composite's active weight from 0.80 to 0.90 with ZERO
    change in deck quality (D23). D69/D70 then found v2's measured numbers
    were ~90% the pipeline's own CTA text, not the model's writing — so v3
    scopes `filler_count` to exclude `PIPELINE_AUTHORED_TYPES` (D73) and
    removes it from the composite entirely rather than re-admitting a
    rescoped version of the same term (D74). v3 is therefore arithmetically
    identical to v1: the same 5 active terms at the same 0.80 total weight,
    given `code_preserved`'s N/A-on-every-golden-deck status is unchanged.
    `grounding_ratio` and every other raw per-metric value are UNCHANGED by
    any of this; only the composite moves. Never compare a bare composite
    number across a `metric_set_version` boundary — compare that field
    first, or compare the raw metrics `substance_composite` was built from.
    """
    word_count = max(1, metrics.get("word_count", 1))
    filler_density = metrics.get("filler_count", 0) / word_count * 100
    code = metrics.get("code_preserved")

    components: dict[str, float | None] = {
        "grounding_ratio": max(0.0, min(1.0, metrics.get("grounding_ratio", 0.0))),
        "numeric_density": _saturate(metrics.get("numeric_density", 0.0), NUMERIC_DENSITY_TARGET),
        "bare_bullet_ratio": 1.0 - max(0.0, min(1.0, metrics.get("bare_bullet_ratio", 1.0))),
        "proper_noun_density": _saturate(metrics.get("proper_noun_density", 0.0), PROPER_NOUN_DENSITY_TARGET),
        "hook_similarity": 1.0 - max(0.0, min(1.0, hook_similarity)),
        "filler_count": (1.0 - _saturate(filler_density, FILLER_DENSITY_TARGET)
                          if FILLER_COUNT_IN_COMPOSITE else None),
        "code_preserved": None if code is None else (1.0 if code else 0.0),
    }

    score, total_weight = 0.0, 0.0
    for key, value in components.items():
        if value is None:
            continue
        weight = COMPOSITE_WEIGHTS[key]
        score += weight * value
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(score / total_weight * 100, 2)
